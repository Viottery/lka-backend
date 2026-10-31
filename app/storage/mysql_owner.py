"""Single coordinator ownership and transaction fencing for a MySQL authority.

The named lock detects a live coordinator, while the token row fences writers
whose coordinator connection died. Every business transaction holds a shared
lock on the token until commit; a replacement owner changes it exclusively.
Worker claims continue to use domain row locks, never the named lock.
"""
from __future__ import annotations

import hashlib
import threading
import uuid

from app.storage.mysql import MySQLConfig, MySQLOperationalError, _load_driver


def coordinator_lock_name(database: str) -> str:
    return "lka:coordinator:" + hashlib.sha256(database.encode()).hexdigest()[:40]


def install_coordinator_schema(conn) -> None:
    """Called only by the privileged, explicit schema migration command."""
    conn.execute_native("""CREATE TABLE IF NOT EXISTS lka_coordinator_owner (
        owner_id TINYINT NOT NULL PRIMARY KEY,
        token CHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL
    ) ENGINE=InnoDB""")


class MySQLCoordinatorOwner:
    def __init__(
        self,
        config: MySQLConfig,
        *,
        heartbeat_interval_seconds: float = 60,
        session_wait_timeout_seconds: int = 300,
        io_timeout_seconds: float = 5,
    ):
        if not 0 < heartbeat_interval_seconds <= 300:
            raise ValueError("coordinator heartbeat interval must be in (0, 300] seconds")
        if type(session_wait_timeout_seconds) is not int or session_wait_timeout_seconds < 300:
            raise ValueError("coordinator session wait timeout must be at least 300 seconds")
        if heartbeat_interval_seconds * 2 >= session_wait_timeout_seconds:
            raise ValueError("coordinator heartbeat needs margin below the session idle timeout")
        if not 0 < io_timeout_seconds <= 5:
            raise ValueError("coordinator I/O timeout must be in (0, 5] seconds")
        self.config = config
        self._heartbeat_interval_seconds = heartbeat_interval_seconds
        self._session_wait_timeout_seconds = session_wait_timeout_seconds
        self._io_timeout_seconds = io_timeout_seconds
        # _lock protects only the published in-memory ownership snapshot.
        self._lock = threading.RLock()
        # Network I/O on the raw GET_LOCK session is serialized separately.
        self._io_lock = threading.Lock()
        self._connection = None
        self._token = None
        self._connection_id = None
        self._state = "inactive"
        self._close_failed = False
        self._heartbeat_stop = None
        self._heartbeat_thread = None
        self._name = coordinator_lock_name(config.database)

    def acquire(self) -> bool:
        with self._lock:
            if self._connection is not None:
                # A lost raw session is never replaced or reacquired by its
                # heartbeat. Only a new owner object can establish ownership.
                return self._state == "active"
            if self._state == "lost":
                return False
            raw = _load_driver().connect(
                host=self.config.host, port=self.config.port, user=self.config.user,
                password=self.config.password, database=self.config.database,
                charset="utf8mb4", autocommit=True,
                connect_timeout=min(self.config.connect_timeout, self._io_timeout_seconds),
                read_timeout=min(self.config.read_timeout, self._io_timeout_seconds),
                write_timeout=self._io_timeout_seconds,
            )
            acquired = False
            try:
                with raw.cursor() as cursor:
                    cursor.execute("SELECT GET_LOCK(%s,0)", (self._name,))
                    if cursor.fetchone()[0] != 1:
                        return False
                    acquired = True
                    cursor.execute("SELECT CONNECTION_ID()")
                    connection_id = cursor.fetchone()[0]
                    cursor.execute("SET SESSION innodb_lock_wait_timeout=%s", (self.config.lock_wait_seconds,))
                    cursor.execute("SET SESSION wait_timeout=%s", (self._session_wait_timeout_seconds,))
                    cursor.execute("SELECT @@SESSION.wait_timeout")
                    if int(cursor.fetchone()[0]) < 300:
                        raise RuntimeError("MySQL coordinator session wait_timeout is below 300 seconds")
                    token = uuid.uuid4().hex
                    raw.begin()
                    cursor.execute("INSERT INTO lka_coordinator_owner(owner_id,token) VALUES(1,%s) "
                                   "ON DUPLICATE KEY UPDATE token=VALUES(token)", (token,))
                    raw.commit()
                self._connection, self._token = raw, token
                self._connection_id = connection_id
                self._state = "active"
                self._heartbeat_stop = threading.Event()
                self._heartbeat_thread = threading.Thread(
                    target=self._heartbeat_loop,
                    args=(raw, self._heartbeat_stop),
                    name=f"lka-mysql-owner-heartbeat-{connection_id}",
                    daemon=True,
                )
                self._heartbeat_thread.start()
                return True
            finally:
                if self._connection is not raw:
                    if acquired:
                        raw.rollback()
                    raw.close()

    def _heartbeat_loop(self, raw, stop: threading.Event) -> None:
        while not stop.wait(self._heartbeat_interval_seconds):
            lost = False
            try:
                with self._io_lock:
                    with self._lock:
                        if self._state != "active" or self._connection is not raw:
                            return
                    # This query both refreshes the idle session and verifies
                    # GET_LOCK on the same connection. No reconnect path exists.
                    with raw.cursor() as cursor:
                        cursor.execute("SELECT IS_USED_LOCK(%s)=CONNECTION_ID()", (self._name,))
                        held = cursor.fetchone()[0]
                    if held != 1:
                        lost = True
            except Exception:  # noqa: BLE001 - every heartbeat failure loses ownership
                lost = True
            if not lost:
                continue
            with self._io_lock:
                with self._lock:
                    if self._connection is not raw or self._state != "active":
                        return
                    self._state = "lost"
                try:
                    if raw.open:
                        raw.close()
                except Exception:  # noqa: BLE001 - retain the lost state if best-effort close fails
                    with self._lock:
                        self._close_failed = True
            return

    @property
    def is_active(self) -> bool:
        with self._lock:
            return self._state == "active" and self._connection is not None

    @property
    def ownership_lost(self) -> bool:
        with self._lock:
            return self._state == "lost"

    def guard_transaction(self, conn) -> None:
        token, owner_connection_id = self.fence_identity()
        row = conn.execute_native(
            "SELECT token,IS_USED_LOCK(%s) FROM lka_coordinator_owner WHERE owner_id=1 FOR SHARE",
            (self._name,),
        ).fetchone()
        if row is None or row[0] != token or row[1] != owner_connection_id:
            raise MySQLOperationalError("MySQL coordinator generation changed")

    def fence_identity(self) -> tuple[str, int]:
        """Snapshot local identity without blocking on network I/O."""
        with self._lock:
            if self._state != "active" or self._connection is None or self._token is None:
                raise MySQLOperationalError("MySQL coordinator is not active")
            return self._token, self._connection_id

    def guard_checkpoint_cursor(self, cursor) -> None:
        token, owner_connection_id = self.fence_identity()
        cursor.execute(
            "SELECT token,IS_USED_LOCK(%s) AS connection_id "
            "FROM lka_coordinator_owner WHERE owner_id=1 FOR SHARE", (self._name,),
        )
        row = cursor.fetchone()
        if row is None or row["token"] != token or row["connection_id"] != owner_connection_id:
            raise MySQLOperationalError("MySQL checkpoint coordinator generation changed")

    async def guard_async_checkpoint_cursor(self, cursor) -> None:
        token, owner_connection_id = self.fence_identity()
        await cursor.execute(
            "SELECT token,IS_USED_LOCK(%s) AS connection_id "
            "FROM lka_coordinator_owner WHERE owner_id=1 FOR SHARE", (self._name,),
        )
        row = await cursor.fetchone()
        if row is None or row["token"] != token or row["connection_id"] != owner_connection_id:
            raise MySQLOperationalError("MySQL checkpoint coordinator generation changed")

    def release(self) -> None:
        with self._lock:
            raw = self._connection
            stop = self._heartbeat_stop
            thread = self._heartbeat_thread
            if self._state != "lost":
                self._state = "released"
            if stop is not None:
                stop.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=self._io_timeout_seconds + 1)
        close_error = None
        try:
            if raw is not None:
                with self._io_lock:
                    if raw.open:
                        raw.close()  # connection close releases GET_LOCK
        except Exception as exc:  # noqa: BLE001 - report inability to release the owner session
            close_error = exc
        finally:
            with self._lock:
                self._connection = self._token = None
                self._connection_id = None
                self._heartbeat_stop = None
                self._heartbeat_thread = None
                if close_error is None:
                    self._close_failed = False
        if close_error is not None:
            raise MySQLOperationalError("MySQL coordinator session could not be closed") from close_error
