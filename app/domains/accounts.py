"""Trusted coadministrator accounts for one personal dataset; not tenant isolation."""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import sqlite3
import time
from collections.abc import Callable
from contextlib import contextmanager

from pydantic import BaseModel, ConfigDict, Field, field_validator


class AccountConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    require_https: bool = True
    secure_cookie: bool = True
    cookie_name: str = Field(default="lka_session", pattern=r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
    session_ttl_seconds: int = Field(default=43200, ge=300, le=604800)
    allowed_origins: list[str] = Field(default_factory=list)
    allow_bearer_sessions: bool = False
    login_max_attempts: int = Field(default=5, ge=1, le=100)
    login_window_seconds: int = Field(default=900, ge=60, le=86400)

    @field_validator("allowed_origins")
    @classmethod
    def origins(cls, values):
        from urllib.parse import urlsplit
        for value in values:
            parts = urlsplit(value)
            if (parts.scheme not in {"http", "https"} or not parts.hostname
                    or parts.username or parts.password or parts.path or parts.query
                    or parts.fragment or "*" in value):
                raise ValueError("allowed_origins must be exact HTTP(S) origins without trailing slash")
        return values


class AccountError(ValueError):
    def __init__(self, detail: str, status_code: int = 400):
        super().__init__(detail)
        self.status_code = status_code


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    derived = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 600_000)
    return f"pbkdf2_sha256$600000${salt}${derived.hex()}"


_DUMMY_HASH = _hash_password("dummy-account-password", "00" * 16)


def _verify(password: str, stored: str) -> bool:
    try:
        algorithm, iterations, salt, expected = stored.split("$")
        if algorithm != "pbkdf2_sha256" or iterations != "600000":
            return False
        return hmac.compare_digest(_hash_password(password, salt).rsplit("$", 1)[1], expected)
    except (ValueError, TypeError):
        return False


class AccountService:
    def __init__(self, conn_factory: Callable[[], sqlite3.Connection], config: AccountConfig,
                 *, on_security_change: Callable[[sqlite3.Connection, str], None] | None = None):
        self._conn_factory = conn_factory
        self.config = config
        self.on_security_change = on_security_change
        self.ensure_schema()

    @contextmanager
    def _db(self, *, write=False):
        conn = self._conn_factory()
        conn.row_factory = sqlite3.Row
        try:
            if write:
                conn.execute("BEGIN IMMEDIATE")
            yield conn
            if write:
                conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def ensure_schema(self):
        with self._db(write=True) as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS auth_accounts(
                user_id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL, disabled INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS auth_sessions(
                session_id TEXT PRIMARY KEY, user_id TEXT NOT NULL,
                token_hash TEXT NOT NULL UNIQUE, csrf_hash TEXT NOT NULL,
                created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
                revoked INTEGER NOT NULL DEFAULT 0)""")
            conn.execute("CREATE INDEX IF NOT EXISTS auth_sessions_user ON auth_sessions(user_id)")
            conn.execute("""CREATE TABLE IF NOT EXISTS auth_login_throttle(
                throttle_key TEXT PRIMARY KEY, window_start INTEGER NOT NULL,
                attempts INTEGER NOT NULL)""")

    @staticmethod
    def _username(username: str) -> str:
        if not isinstance(username, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{2,63}", username):
            raise AccountError("Username must be 3..64 ASCII letters, numbers, dots, underscores or hyphens", 422)
        return username.lower()

    @staticmethod
    def _password(password: str):
        if not isinstance(password, str) or not 12 <= len(password) <= 256:
            raise AccountError("Password must be 12..256 characters", 422)

    @staticmethod
    def _public(row):
        return {"user_id": row["user_id"], "username": row["username"],
                "is_admin": True, "disabled": bool(row["disabled"]), "created_at": row["created_at"]}

    def create_admin(self, username: str, password: str, *, actor_user_id: str | None = None):
        username = self._username(username)
        self._password(password)
        password_hash = _hash_password(password)
        with self._db(write=True) as conn:
            if actor_user_id is None:
                if conn.execute("SELECT 1 FROM auth_accounts LIMIT 1").fetchone():
                    raise AccountError("Bootstrap is only allowed before the first account; use an administrator session", 409)
            else:
                self._admin(conn, actor_user_id)
            try:
                conn.execute("INSERT INTO auth_accounts VALUES(?,?,?,0,?)",
                             (secrets.token_hex(16), username, password_hash, int(time.time())))
            except sqlite3.IntegrityError as exc:
                raise AccountError("Username unavailable", 409) from exc
            return self._public(conn.execute("SELECT * FROM auth_accounts WHERE username=?", (username,)).fetchone())

    @staticmethod
    def _admin(conn, user_id):
        row = conn.execute("SELECT * FROM auth_accounts WHERE user_id=? AND disabled=0", (user_id,)).fetchone()
        if row is None:
            raise AccountError("Administrator authentication required", 401)
        return row

    def _reserve_login(self, username: str, source: str):
        now = int(time.time())
        keys = [(_digest("user:" + username), self.config.login_max_attempts),
                (_digest("source:" + source), self.config.login_max_attempts * 5)]
        with self._db(write=True) as conn:
            conn.execute("DELETE FROM auth_login_throttle WHERE window_start<?", (now - self.config.login_window_seconds,))
            for key, limit in keys:
                row = conn.execute("SELECT * FROM auth_login_throttle WHERE throttle_key=?", (key,)).fetchone()
                if row and row["attempts"] >= limit:
                    raise AccountError("Too many login attempts; try later", 429)
            for key, _ in keys:
                conn.execute("""INSERT INTO auth_login_throttle VALUES(?,?,1)
                    ON CONFLICT(throttle_key) DO UPDATE SET attempts=attempts+1""", (key, now))
        return keys

    def login(self, username: str, password: str, source: str):
        # Both invalid and unknown usernames receive the same expensive verification.
        normalized = username.lower()
        keys = self._reserve_login(normalized, source)
        with self._db() as conn:
            row = conn.execute("SELECT * FROM auth_accounts WHERE username=?", (normalized,)).fetchone()
        stored = row["password_hash"] if row else _DUMMY_HASH
        valid = _verify(password, stored)
        if not valid or row is None or row["disabled"]:
            raise AccountError("Invalid username or password", 401)
        token, session_id = secrets.token_urlsafe(32), secrets.token_hex(16)
        # Derived from the opaque session credential so any tab holding the
        # HttpOnly cookie can recover it through /auth/me; only its hash is stored.
        csrf = self.csrf_for(token)
        now = int(time.time())
        with self._db(write=True) as conn:
            current = self._admin(conn, row["user_id"])
            if current["password_hash"] != stored:
                raise AccountError("Invalid username or password", 401)
            conn.execute("INSERT INTO auth_sessions VALUES(?,?,?,?,?,?,0)",
                         (session_id, row["user_id"], _digest(token), _digest(csrf), now,
                          now + self.config.session_ttl_seconds))
            for key, _ in keys:
                conn.execute("UPDATE auth_login_throttle SET attempts=MAX(0,attempts-1) WHERE throttle_key=?", (key,))
        return {"account": self._public(row), "session_id": session_id, "token": token,
                "csrf_token": csrf, "expires_at": now + self.config.session_ttl_seconds}

    def authenticate(self, token: str):
        if not token or len(token) > 512:
            return None
        with self._db() as conn:
            row = conn.execute("""SELECT a.*,s.session_id,s.csrf_hash,s.expires_at FROM auth_sessions s
                JOIN auth_accounts a ON a.user_id=s.user_id WHERE s.token_hash=?
                AND s.revoked=0 AND a.disabled=0 AND s.expires_at>?""", (_digest(token), int(time.time()))).fetchone()
        if row is None:
            return None
        return {**self._public(row), "session_id": row["session_id"],
                "csrf_hash": row["csrf_hash"], "expires_at": row["expires_at"]}

    @staticmethod
    def check_csrf(account: dict, csrf: str):
        return bool(csrf) and len(csrf) <= 512 and hmac.compare_digest(account["csrf_hash"], _digest(csrf))

    @staticmethod
    def csrf_for(session_token: str) -> str:
        """Recoverable only with the session credential itself (HttpOnly cookie)."""
        return hmac.new(session_token.encode(), b"lka-account-csrf-v1", hashlib.sha256).hexdigest()

    def list_accounts(self, actor_user_id: str):
        with self._db() as conn:
            self._admin(conn, actor_user_id)
            return [self._public(row) for row in conn.execute("SELECT * FROM auth_accounts ORDER BY created_at,user_id")]

    def list_sessions(self, user_id: str):
        with self._db() as conn:
            self._admin(conn, user_id)
            return [dict(row) for row in conn.execute("""SELECT session_id,created_at,expires_at FROM auth_sessions
                WHERE user_id=? AND revoked=0 AND expires_at>? ORDER BY created_at""", (user_id, int(time.time())))]

    def revoke_session(self, user_id: str, session_id: str):
        with self._db(write=True) as conn:
            self._admin(conn, user_id)
            conn.execute("UPDATE auth_sessions SET revoked=1 WHERE user_id=? AND session_id=?", (user_id, session_id))

    def change_password(self, user_id: str, old_password: str, new_password: str):
        self._password(new_password)
        with self._db() as conn:
            row = self._admin(conn, user_id)
        if not _verify(old_password, row["password_hash"]):
            raise AccountError("Invalid current password", 403)
        new_hash = _hash_password(new_password)
        with self._db(write=True) as conn:
            current = self._admin(conn, user_id)
            if current["password_hash"] != row["password_hash"]:
                raise AccountError("Password changed; sign in again", 409)
            conn.execute("UPDATE auth_accounts SET password_hash=? WHERE user_id=?", (new_hash, user_id))
            conn.execute("UPDATE auth_sessions SET revoked=1 WHERE user_id=?", (user_id,))
            if self.on_security_change:
                self.on_security_change(conn, user_id)

    def reset_password(self, actor_user_id: str, user_id: str, new_password: str):
        self._password(new_password)
        new_hash = _hash_password(new_password)
        with self._db(write=True) as conn:
            self._admin(conn, actor_user_id)
            if not conn.execute("SELECT 1 FROM auth_accounts WHERE user_id=?", (user_id,)).fetchone():
                raise AccountError("Account not found", 404)
            conn.execute("UPDATE auth_accounts SET password_hash=? WHERE user_id=?", (new_hash, user_id))
            conn.execute("UPDATE auth_sessions SET revoked=1 WHERE user_id=?", (user_id,))
            if self.on_security_change:
                self.on_security_change(conn, user_id)

    def set_disabled(self, actor_user_id: str, user_id: str, disabled: bool):
        with self._db(write=True) as conn:
            self._admin(conn, actor_user_id)
            row = conn.execute("SELECT * FROM auth_accounts WHERE user_id=?", (user_id,)).fetchone()
            if row is None:
                raise AccountError("Account not found", 404)
            if disabled and not row["disabled"] and conn.execute("SELECT COUNT(*) FROM auth_accounts WHERE disabled=0").fetchone()[0] <= 1:
                raise AccountError("Cannot disable the last active administrator", 409)
            conn.execute("UPDATE auth_accounts SET disabled=? WHERE user_id=?", (int(disabled), user_id))
            if disabled:
                conn.execute("UPDATE auth_sessions SET revoked=1 WHERE user_id=?", (user_id,))
                if self.on_security_change:
                    self.on_security_change(conn, user_id)
