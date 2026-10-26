"""One-use node admission grants and independent, locally persisted client sessions.

The hub never needs the node's credential plaintext. It sends grant hashes over
the existing authenticated node poll; only the client receives the grant secret.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import sqlite3
import stat
import time
from collections.abc import Callable
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.domains.workspace_mounts import WorkspaceMountConfig
from app.platform.safe_files import is_link_or_reparse, scandir_no_follow


class DeviceConnectionConfig(BaseModel):
    enabled: bool = False
    node_id: str = Field(default="", max_length=120)
    allowed_origins: list[str] = Field(default_factory=list, max_length=30)
    workspace_roots: dict[str, str] = Field(default_factory=dict, max_length=100)
    workspace_mounts: dict[str, WorkspaceMountConfig] = Field(default_factory=dict, max_length=100)
    filesystem_browser_enabled: bool = False
    grant_ttl_seconds: int = Field(default=120, ge=15, le=300)
    session_ttl_seconds: int = Field(default=2592000, ge=60, le=7776000)
    allow_native: bool = True
    cookie_name: str = Field(default="lka_node_connection", pattern=r"^[A-Za-z0-9_-]+$")

    @field_validator("allowed_origins")
    @classmethod
    def origins(cls, values):
        for value in values:
            parsed = urlsplit(value)
            if (parsed.scheme != "https" or not parsed.hostname or parsed.username
                    or parsed.password or parsed.path or parsed.query or parsed.fragment
                    or value != f"https://{parsed.netloc}"):
                raise ValueError("connection origins must be explicit HTTPS origins")
        return values

    @field_validator("workspace_roots")
    @classmethod
    def roots(cls, values):
        if any(not identity or not Path(root).is_absolute() for identity, root in values.items()):
            raise ValueError("workspace mappings require an ID and an absolute local path")
        return values

    @model_validator(mode="after")
    def configured_node(self):
        if self.workspace_mounts.keys() - self.workspace_roots.keys():
            raise ValueError("workspace mounts require an explicit workspace root mapping")
        if self.enabled and self.node_id and not self.allowed_origins:
            raise ValueError("a node connection origin must be configured")
        return self


class ConnectionGrant(BaseModel):
    model_config = ConfigDict(extra="forbid")
    grant_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    token_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    node_id: str = Field(min_length=1, max_length=120)
    user_id: str = Field(min_length=1, max_length=200)
    workspace_ids: list[str] = Field(max_length=100)
    expires_at: float
    authorization_epoch: int = Field(default=1, strict=True, ge=1)


class ConnectionAuthorization(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node_id: str = Field(min_length=1, max_length=120)
    user_id: str = Field(min_length=1, max_length=200)
    authorization_epoch: int = Field(strict=True, ge=1)


class ActiveSessionRunError(RuntimeError):
    """A session workspace cannot change while an existing run is resumable."""


class DeviceConnectionService:
    def __init__(self, conn_factory: Callable[[], sqlite3.Connection],
                 config: DeviceConnectionConfig, *, device_service=None, clock=time.time):
        self._conn_factory, self.config = conn_factory, config
        self.devices, self._clock = device_service, clock
        with self._connection() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS device_connection_grants (
                    grant_id TEXT PRIMARY KEY, token_hash TEXT NOT NULL UNIQUE,
                    node_id TEXT NOT NULL, user_id TEXT NOT NULL, workspace_ids TEXT NOT NULL,
                    expires_at REAL NOT NULL, consumed INTEGER NOT NULL DEFAULT 0,
                    credential_version INTEGER);
                CREATE TABLE IF NOT EXISTS device_connection_sessions (
                    session_id TEXT PRIMARY KEY, token_hash TEXT NOT NULL UNIQUE,
                    csrf_hash TEXT NOT NULL, node_id TEXT NOT NULL, user_id TEXT NOT NULL,
                    workspace_ids TEXT NOT NULL, expires_at REAL NOT NULL,
                    revoked INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS device_connection_session_policies (
                    session_id TEXT PRIMARY KEY, workspace_ids TEXT NOT NULL,
                    allowed_paths TEXT NOT NULL DEFAULT '[]', node_id TEXT NOT NULL DEFAULT '',
                    user_id TEXT NOT NULL DEFAULT '', authorization_epoch INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS device_connection_workspace_selections (
                    node_id TEXT NOT NULL, user_id TEXT NOT NULL, authorization_epoch INTEGER NOT NULL,
                    workspace_id TEXT NOT NULL, path TEXT NOT NULL, created_at REAL NOT NULL,
                    PRIMARY KEY(node_id,user_id,authorization_epoch,workspace_id));
                CREATE TABLE IF NOT EXISTS device_connection_authorizations (
                    node_id TEXT NOT NULL, user_id TEXT NOT NULL,
                    authorization_epoch INTEGER NOT NULL, PRIMARY KEY(node_id,user_id));
                CREATE TABLE IF NOT EXISTS device_connection_grant_epochs (
                    grant_id TEXT PRIMARY KEY, authorization_epoch INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS device_connection_session_epochs (
                    session_id TEXT PRIMARY KEY, authorization_epoch INTEGER NOT NULL);
            """)
            policy_columns = {row[1] for row in conn.execute(
                "PRAGMA table_info(device_connection_session_policies)")}
            if "allowed_paths" not in policy_columns:
                conn.execute("ALTER TABLE device_connection_session_policies "
                             "ADD COLUMN allowed_paths TEXT NOT NULL DEFAULT '[]'")
            for name, declaration in (("node_id", "TEXT NOT NULL DEFAULT ''"),
                                      ("user_id", "TEXT NOT NULL DEFAULT ''"),
                                      ("authorization_epoch", "INTEGER NOT NULL DEFAULT 0")):
                if name not in policy_columns:
                    conn.execute(f"ALTER TABLE device_connection_session_policies ADD COLUMN {name} {declaration}")

    @contextmanager
    def _connection(self, *, write=False):
        conn = self._conn_factory()
        conn.row_factory = sqlite3.Row
        try:
            if write:
                conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _hash(token):
        return hashlib.sha256(token.encode()).hexdigest()

    def _enabled(self, *, node=False):
        if not self.config.enabled or node and not self.config.node_id:
            raise PermissionError("device connections disabled")

    @staticmethod
    def _epoch(conn, node_id, user_id):
        row = conn.execute("SELECT authorization_epoch FROM device_connection_authorizations "
                           "WHERE node_id=? AND user_id=?", (node_id, user_id)).fetchone()
        return row[0] if row else 1

    def _require_active_principal(self, conn, principal):
        epoch = self._epoch(conn, principal["node_id"], principal["user_id"])
        session = conn.execute(
            "SELECT revoked,expires_at FROM device_connection_sessions WHERE session_id=? "
            "AND node_id=? AND user_id=?",
            (principal.get("session_id"), principal["node_id"], principal["user_id"]),
        ).fetchone()
        if (epoch != principal.get("authorization_epoch") or session is None
                or session["revoked"] or session["expires_at"] <= self._clock()):
            raise PermissionError("device connection authorization is no longer active")

    @staticmethod
    def _set_epoch(conn, node_id, user_id, epoch):
        conn.execute("INSERT INTO device_connection_authorizations VALUES(?,?,?) "
                     "ON CONFLICT(node_id,user_id) DO UPDATE SET "
                     "authorization_epoch=max(authorization_epoch,excluded.authorization_epoch)",
                     (node_id, user_id, epoch))
        conn.execute("UPDATE device_connection_grants SET consumed=1 WHERE node_id=? AND user_id=? "
                     "AND coalesce((SELECT authorization_epoch FROM device_connection_grant_epochs e "
                     "WHERE e.grant_id=device_connection_grants.grant_id),1)<?", (node_id, user_id, epoch))
        conn.execute("UPDATE device_connection_sessions SET revoked=1 WHERE node_id=? AND user_id=? "
                     "AND coalesce((SELECT authorization_epoch FROM device_connection_session_epochs e "
                     "WHERE e.session_id=device_connection_sessions.session_id),1)<?", (node_id, user_id, epoch))
        conn.execute("DELETE FROM device_connection_workspace_selections "
                     "WHERE node_id=? AND user_id=? AND authorization_epoch<?",
                     (node_id, user_id, epoch))

    def bump_account_epoch(self, conn, user_id):
        """Called inside the account security-change transaction; never commits."""
        nodes = conn.execute("SELECT node_id FROM device_connection_authorizations WHERE user_id=? "
                             "UNION SELECT node_id FROM device_connection_grants WHERE user_id=? "
                             "UNION SELECT node_id FROM device_connection_sessions WHERE user_id=?",
                             (user_id, user_id, user_id)).fetchall()
        for row in nodes:
            self._set_epoch(conn, row[0], user_id, self._epoch(conn, row[0], user_id) + 1)

    def revoke_authorization(self, user_id, node_id):
        self._enabled()
        with self._connection(write=True) as conn:
            if not self.devices or not conn.execute("SELECT 1 FROM cloud_devices WHERE device_id=?", (node_id,)).fetchone():
                raise PermissionError("approved device required")
            epoch = self._epoch(conn, node_id, user_id) + 1
            self._set_epoch(conn, node_id, user_id, epoch)
        return {"node_id": node_id, "user_id": user_id, "authorization_epoch": epoch}

    def issue_grant(self, user_id, node_id, workspace_ids=None):
        self._enabled()
        if not self.devices or not user_id:
            raise PermissionError("hub account required")
        token, identity = secrets.token_urlsafe(48), secrets.token_hex(16)
        expires = self._clock() + self.config.grant_ttl_seconds
        with self._connection(write=True) as conn:
            row = conn.execute("SELECT * FROM cloud_devices WHERE device_id=? AND revoked=0",
                               (node_id,)).fetchone()
            if row is None:
                raise PermissionError("approved device required")
            profile = json.loads(row["profile"])
            origin = profile.get("advertised_url")
            if not origin:
                raise ValueError("device has no approved direct connection origin")
            approved = profile["workspace_ids"]
            selected = approved if workspace_ids is None else workspace_ids
            if len(selected) != len(set(selected)) or set(selected) - set(approved):
                raise PermissionError("workspace is not approved for device")
            conn.execute("DELETE FROM device_connection_grants WHERE expires_at<=?",
                         (self._clock(),))
            count = conn.execute("SELECT count(*) FROM device_connection_grants WHERE node_id=?",
                                 (node_id,)).fetchone()[0]
            if count >= 100:
                raise ValueError("too many pending connection grants")
            conn.execute("INSERT INTO device_connection_grants VALUES(?,?,?,?,?,?,0,?)",
                         (identity, self._hash(token), node_id, user_id, json.dumps(selected),
                          expires, row["credential_version"]))
            epoch = self._epoch(conn, node_id, user_id)
            conn.execute("INSERT INTO device_connection_grant_epochs VALUES(?,?)", (identity, epoch))
            self._set_epoch(conn, node_id, user_id, epoch)
        return {"grant": token, "grant_id": identity, "node_id": node_id,
                "origin": origin.rstrip("/"), "expires_at": expires,
                "scopes": ["ordinary.chat", "workspaces"],
                "delivery": "pending_node_poll"}

    def pending_grants(self, principal):
        self._enabled()
        with self._connection() as conn:
            profile = self.devices.require_node(conn, principal, "connections.poll")
            rows = conn.execute("SELECT g.*,coalesce(e.authorization_epoch,1) AS grant_epoch "
                                "FROM device_connection_grants g LEFT JOIN device_connection_grant_epochs e "
                                "ON e.grant_id=g.grant_id WHERE node_id=? AND consumed=0 "
                                "AND credential_version=? AND expires_at>? ORDER BY g.grant_id LIMIT 100",
                                (principal["node_id"], principal["credential_version"], self._clock()))
            return {"items": [{"grant_id": row["grant_id"], "token_hash": row["token_hash"],
                               "node_id": row["node_id"], "user_id": row["user_id"],
                               "workspace_ids": json.loads(row["workspace_ids"]),
                               "expires_at": row["expires_at"], "authorization_epoch": row["grant_epoch"]} for row in rows
                              if not set(json.loads(row["workspace_ids"]))
                              - set(profile["workspace_ids"])
                              and row["grant_epoch"] == self._epoch(conn, row["node_id"], row["user_id"])],
                    "authorizations": [dict(row) for row in conn.execute(
                        "SELECT node_id,user_id,authorization_epoch FROM device_connection_authorizations "
                        "WHERE node_id=? ORDER BY user_id", (principal["node_id"],))]}

    def install_grants(self, items, authorizations=None):
        """Only call with data obtained from the pinned, authenticated hub transport."""
        self._enabled(node=True)
        if not isinstance(items, list) or len(items) > 100:
            raise ValueError("invalid connection grant page")
        grants = [ConnectionGrant.model_validate(item) for item in items]
        if authorizations is not None and (not isinstance(authorizations, list) or len(authorizations) > 10000):
            raise ValueError("invalid connection authorization page")
        epochs = [ConnectionAuthorization.model_validate(item) for item in authorizations or []]
        with self._connection(write=True) as conn:
            for authorization in epochs:
                if authorization.node_id != self.config.node_id:
                    raise PermissionError("connection authorization audience mismatch")
                current = self._epoch(conn, authorization.node_id, authorization.user_id)
                self._set_epoch(conn, authorization.node_id, authorization.user_id,
                                max(current, authorization.authorization_epoch))
            conn.execute("DELETE FROM device_connection_grants WHERE expires_at<=?", (self._clock(),))
            for grant in grants:
                if grant.node_id != self.config.node_id:
                    raise PermissionError("connection grant audience mismatch")
                if set(grant.workspace_ids) - self.config.workspace_roots.keys():
                    raise PermissionError("connection grant workspace is not mapped locally")
                if not self._clock() < grant.expires_at <= self._clock() + 300:
                    raise ValueError("connection grant expired or invalid lifetime")
                current = self._epoch(conn, grant.node_id, grant.user_id)
                if grant.authorization_epoch < current:
                    continue
                if grant.authorization_epoch > current:
                    raise PermissionError("connection grant authorization epoch not installed")
                old = conn.execute("SELECT token_hash FROM device_connection_grants WHERE grant_id=?",
                                   (grant.grant_id,)).fetchone()
                if old and old[0] != grant.token_hash:
                    raise PermissionError("connection grant identity changed")
                conn.execute("INSERT OR IGNORE INTO device_connection_grants VALUES(?,?,?,?,?,?,0,NULL)",
                             (grant.grant_id, grant.token_hash, grant.node_id, grant.user_id,
                              json.dumps(grant.workspace_ids), grant.expires_at))
                conn.execute("INSERT OR IGNORE INTO device_connection_grant_epochs VALUES(?,?)",
                             (grant.grant_id, grant.authorization_epoch))
        return len(grants)

    def redeem(self, token):
        self._enabled(node=True)
        if not isinstance(token, str) or not 32 <= len(token) <= 256:
            raise PermissionError("invalid or expired connection grant")
        session_token = secrets.token_urlsafe(48)
        csrf = self.csrf_for(session_token)
        identity = secrets.token_hex(16)
        expires = self._clock() + self.config.session_ttl_seconds
        with self._connection(write=True) as conn:
            row = conn.execute("SELECT * FROM device_connection_grants WHERE token_hash=? "
                               "AND node_id=? AND consumed=0 AND expires_at>?",
                               (self._hash(token), self.config.node_id, self._clock())).fetchone()
            if row is None:
                raise PermissionError("invalid or expired connection grant")
            epoch_row = conn.execute("SELECT authorization_epoch FROM device_connection_grant_epochs WHERE grant_id=?",
                                     (row["grant_id"],)).fetchone()
            epoch = epoch_row[0] if epoch_row else 1
            if epoch != self._epoch(conn, row["node_id"], row["user_id"]):
                raise PermissionError("connection authorization revoked")
            if set(json.loads(row["workspace_ids"])) - self.config.workspace_roots.keys():
                raise PermissionError("connection grant workspace is not mapped locally")
            conn.execute("UPDATE device_connection_grants SET consumed=1 WHERE grant_id=?",
                         (row["grant_id"],))
            conn.execute("INSERT INTO device_connection_sessions VALUES(?,?,?,?,?,?,?,0)",
                         (identity, self._hash(session_token), self._hash(csrf), self.config.node_id,
                          row["user_id"], row["workspace_ids"], expires))
            conn.execute("INSERT INTO device_connection_session_epochs VALUES(?,?)", (identity, epoch))
        return {"token": session_token, "csrf_token": csrf, "session_id": identity,
                "node_id": self.config.node_id, "expires_at": expires,
                "scopes": ["ordinary.chat", "workspaces"]}

    def authenticate(self, token):
        if not self.config.enabled or not self.config.node_id or not token or len(token) > 256:
            return None
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM device_connection_sessions WHERE token_hash=? "
                               "AND node_id=? AND revoked=0 AND expires_at>?",
                               (self._hash(token), self.config.node_id, self._clock())).fetchone()
            if row is None:
                return None
            epoch = conn.execute("SELECT authorization_epoch FROM device_connection_session_epochs WHERE session_id=?",
                                 (row["session_id"],)).fetchone()
            if (epoch[0] if epoch else 1) != self._epoch(conn, row["node_id"], row["user_id"]):
                return None
            workspaces = json.loads(row["workspace_ids"])
            if set(workspaces) - self.config.workspace_roots.keys():
                return None
            workspaces.extend(item[0] for item in conn.execute(
                "SELECT workspace_id FROM device_connection_workspace_selections "
                "WHERE node_id=? AND user_id=? AND authorization_epoch=? ORDER BY created_at DESC LIMIT 100",
                (row["node_id"], row["user_id"], epoch[0] if epoch else 1),
            ) if item[0] not in workspaces)
            return {"session_id": row["session_id"], "node_id": row["node_id"],
                    "user_id": row["user_id"], "workspace_ids": workspaces,
                    "expires_at": row["expires_at"], "csrf_hash": row["csrf_hash"],
                    "authorization_epoch": epoch[0] if epoch else 1}

    def check_csrf(self, principal, token):
        return bool(token and hmac.compare_digest(principal["csrf_hash"], self._hash(token)))

    @staticmethod
    def csrf_for(session_token):
        # Recoverable only from the request's authenticated opaque credential.
        # The DB still stores hashes, and a new browser tab can work offline.
        return hmac.new(session_token.encode(), b"lka-node-csrf-v1", hashlib.sha256).hexdigest()

    def revoke(self, session_id):
        with self._connection(write=True) as conn:
            conn.execute("UPDATE device_connection_sessions SET revoked=1 WHERE session_id=? AND node_id=?",
                         (session_id, self.config.node_id))

    def list_sessions(self, user_id, current_session_id=None):
        self._enabled(node=True)
        with self._connection() as conn:
            rows = conn.execute("SELECT s.*,coalesce(e.authorization_epoch,1) AS session_epoch "
                                "FROM device_connection_sessions s LEFT JOIN device_connection_session_epochs e "
                                "ON e.session_id=s.session_id WHERE node_id=? AND user_id=? "
                                "AND revoked=0 AND expires_at>? ORDER BY expires_at DESC LIMIT 1000",
                                (self.config.node_id, user_id, self._clock()))
            epoch = self._epoch(conn, self.config.node_id, user_id)
            return [{"session_id": row["session_id"], "node_id": row["node_id"],
                     "expires_at": row["expires_at"], "workspace_ids": json.loads(row["workspace_ids"]),
                     "current": row["session_id"] == current_session_id}
                    for row in rows if row["session_epoch"] == epoch]

    def revoke_session(self, user_id, session_id):
        self._enabled(node=True)
        with self._connection(write=True) as conn:
            conn.execute("UPDATE device_connection_sessions SET revoked=1 "
                         "WHERE session_id=? AND user_id=? AND node_id=?",
                         (session_id, user_id, self.config.node_id))

    def bind_session_policy(self, session_id, principal):
        """Freeze the selected session path; idle clients may explicitly rebind it."""
        self._enabled(node=True)
        if principal.get("node_id") != self.config.node_id:
            raise PermissionError("connection policy audience mismatch")
        selected = set()
        allowed_paths = []
        # Agent authority follows the workspace explicitly bound to this session.
        # A connection may browse/select many directories without granting them all
        # to every Agent session opened from that connection.
        with self._connection() as conn:
            row = conn.execute("SELECT metadata FROM agent_sessions WHERE session_id=?", (session_id,)).fetchone()
        if row is not None:
            try:
                metadata = json.loads(row[0])
                path = metadata.get("workspace", {}).get("backend_path")
                if isinstance(path, str) and path:
                    mapped = self.workspace_roots(principal)
                    target = Path(path).resolve()
                    normalized_roots = {identity: Path(root).resolve()
                                        for identity, root in mapped.items()}
                    selected = {identity for identity, root in mapped.items()
                                if target.is_relative_to(normalized_roots[identity])}
                    if selected:
                        allowed_paths = [str(target)]
                else:
                    mapped, normalized_roots = {}, {}
            except (OSError, RuntimeError, ValueError, TypeError):
                selected, mapped, normalized_roots = set(), {}, {}
        else:
            mapped, normalized_roots = {}, {}
        with self._connection(write=True) as conn:
            self._require_active_principal(conn, principal)
            previous = conn.execute("SELECT workspace_ids,allowed_paths FROM device_connection_session_policies "
                                    "WHERE session_id=?", (session_id,)).fetchone()
            if previous:
                active_run = conn.execute(
                    "SELECT 1 FROM agent_runs WHERE session_id=? AND status NOT IN "
                    "('completed','failed','timed_out','cancelled') LIMIT 1", (session_id,),
                ).fetchone()
                if active_run:
                    selected &= set(json.loads(previous[0]))
                    allowed_paths = json.loads(previous[1])
            current_roots = self._workspace_roots_in(conn, principal)
            selected = {identity for identity in selected
                        if identity in current_roots and current_roots[identity] == mapped.get(identity)}
            allowed_paths = [path for path in allowed_paths if any(
                Path(path).is_relative_to(normalized_roots[identity])
                for identity in selected
            )]
            conn.execute("INSERT INTO device_connection_session_policies "
                         "(session_id,workspace_ids,allowed_paths,node_id,user_id,authorization_epoch) "
                         "VALUES(?,?,?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET "
                         "workspace_ids=excluded.workspace_ids,allowed_paths=excluded.allowed_paths, "
                         "node_id=excluded.node_id,user_id=excluded.user_id, "
                         "authorization_epoch=excluded.authorization_epoch",
                         (session_id, json.dumps(sorted(selected)), json.dumps(sorted(set(allowed_paths))),
                          principal["node_id"], principal["user_id"], principal["authorization_epoch"]))
        return self.session_policy(session_id)

    def session_has_active_run(self, session_id):
        with self._connection() as conn:
            return conn.execute(
                "SELECT 1 FROM agent_runs WHERE session_id=? AND status NOT IN "
                "('completed','failed','timed_out','cancelled') LIMIT 1", (session_id,),
            ).fetchone() is not None

    def bind_session_workspace(self, session_id, principal, workspace_id, workspace, project_id=None):
        """Atomically change an idle session root and its next-run tool scope."""
        if principal.get("node_id") != self.config.node_id:
            raise PermissionError("workspace selection audience mismatch")
        requested_roots = self.workspace_roots(principal)
        requested_root = requested_roots.get(workspace_id)
        if not requested_root:
            raise PermissionError("workspace is not approved for this connection")
        selected_path = Path(requested_root).resolve(strict=True)
        if not selected_path.is_dir():
            raise PermissionError("workspace directory is unavailable")
        workspace_data = workspace.model_dump(mode="json")
        workspace_data["backend_path"] = str(selected_path)
        with self._connection(write=True) as conn:
            self._require_active_principal(conn, principal)
            roots = self._workspace_roots_in(conn, principal)
            root = roots.get(workspace_id)
            if root != requested_root or str(selected_path) != str(Path(root)):
                raise PermissionError("workspace selection changed; select the directory again")
            row = conn.execute("SELECT metadata,status FROM agent_sessions WHERE session_id=?",
                               (session_id,)).fetchone()
            if row is None or row["status"] == "deleted":
                raise KeyError(f"Session not found: {session_id}")
            active = conn.execute(
                "SELECT 1 FROM agent_runs WHERE session_id=? AND status NOT IN "
                "('completed','failed','timed_out','cancelled') LIMIT 1", (session_id,),
            ).fetchone()
            if active:
                raise ActiveSessionRunError("finish or cancel the active Agent run before changing its workspace")
            metadata = json.loads(row["metadata"])
            metadata["workspace"] = workspace_data
            if project_id:
                metadata["project_id"] = project_id
            else:
                metadata.pop("project_id", None)
            conn.execute("UPDATE agent_sessions SET metadata=?,updated_at=? WHERE session_id=?",
                         (json.dumps(metadata, ensure_ascii=False), datetime.now(UTC).isoformat(), session_id))
            conn.execute("INSERT INTO device_connection_session_policies "
                         "(session_id,workspace_ids,allowed_paths,node_id,user_id,authorization_epoch) "
                         "VALUES(?,?,?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET "
                         "workspace_ids=excluded.workspace_ids,allowed_paths=excluded.allowed_paths, "
                         "node_id=excluded.node_id,user_id=excluded.user_id, "
                         "authorization_epoch=excluded.authorization_epoch",
                         (session_id, json.dumps([workspace_id]), json.dumps([str(selected_path)]),
                          principal["node_id"], principal["user_id"], principal["authorization_epoch"]))
        return workspace_data

    def session_policy(self, session_id):
        with self._connection() as conn:
            row = conn.execute("SELECT workspace_ids,allowed_paths,node_id,user_id,authorization_epoch "
                               "FROM device_connection_session_policies "
                               "WHERE session_id=?", (session_id,)).fetchone()
            selected = json.loads(row[0]) if row is not None else []
            allowed_paths = json.loads(row[1]) if row is not None else []
            identity = ({"node_id": row[2], "user_id": row[3], "authorization_epoch": row[4]}
                        if row is not None else None)
            if identity and identity["node_id"] and (
                identity["node_id"] != self.config.node_id
                or identity["authorization_epoch"] != self._epoch(conn, identity["node_id"], identity["user_id"])
            ):
                allowed_paths = []
                selected = []
            scoped_identity = {**identity, "workspace_ids": selected} if identity and identity["node_id"] else None
            if scoped_identity:
                current_roots = self._workspace_roots_in(conn, scoped_identity)
            else:
                # Pre-identity policy rows may retain only configured mappings.
                # Never resolve an old dynamic ID globally after an upgrade.
                current_roots = {workspace_id: self.config.workspace_roots[workspace_id]
                                 for workspace_id in selected
                                 if workspace_id in self.config.workspace_roots}
            selected = [workspace_id for workspace_id in selected if workspace_id in current_roots]
            if row is not None and not allowed_paths:
                session = conn.execute("SELECT metadata FROM agent_sessions WHERE session_id=?",
                                       (session_id,)).fetchone()
                if session:
                    try:
                        workspace_path = json.loads(session[0]).get("workspace", {}).get("backend_path")
                        if workspace_path:
                            target = Path(workspace_path).resolve()
                            candidates = [current_roots[workspace_id] for workspace_id in selected]
                            if any(target.is_relative_to(Path(root).resolve()) for root in candidates):
                                allowed_paths = [str(target)]
                    except (OSError, RuntimeError, ValueError, TypeError):
                        allowed_paths = []
            roots = [str(Path(path).resolve()) for path in allowed_paths if any(
                Path(path).resolve().is_relative_to(Path(current_roots[workspace_id]).resolve())
                for workspace_id in selected
            )]
        if row is None:
            return None
        return {"allow_default_workspace": False, "allowed_paths": sorted(set(roots)),
                "safety_review_mode": "manual"}

    def workspace_roots(self, principal):
        """Return only the operator mappings and roots selected under this identity."""
        with self._connection() as conn:
            return self._workspace_roots_in(conn, principal)

    def _workspace_roots_in(self, conn, principal):
        roots = {identity: path for identity, path in self.config.workspace_roots.items()
                 if identity in principal.get("workspace_ids", [])}
        if principal.get("node_id") != self.config.node_id:
            return roots
        rows = conn.execute(
            "SELECT workspace_id,path FROM device_connection_workspace_selections "
            "WHERE node_id=? AND user_id=? AND authorization_epoch=? ORDER BY created_at DESC LIMIT 100",
            (self.config.node_id, principal["user_id"], principal["authorization_epoch"]),
        )
        roots.update({row["workspace_id"]: row["path"] for row in rows})
        return roots

    def browse_directories(self, path="", *, query="", offset=0, limit=200):
        """List directory children only; browser access never reads file contents."""
        if not self.config.filesystem_browser_enabled:
            raise PermissionError("full filesystem browser is disabled")
        if (not isinstance(path, str) or len(path) > 4096 or "\x00" in path
                or not isinstance(query, str) or len(query) > 120
                or type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 200):
            raise ValueError("invalid directory path")
        if os.name == "nt":
            import ctypes
            mask = ctypes.windll.kernel32.GetLogicalDrives()
            roots = [f"{chr(65 + i)}:\\" for i in range(26) if mask & (1 << i)]
        else:
            roots = [str(Path("/").resolve())]
        root_items = [{"name": root, "path": root} for root in roots]
        root_page = root_items[offset:offset + limit]
        if not path:
            return {"path": "", "parent": None, "roots": root_page,
                    "directories": [], "next_offset": offset + limit if offset + limit < len(roots) else None,
                    "truncated": False}
        target = Path(path).expanduser()
        if not target.is_absolute():
            raise PermissionError("directory path must be absolute and cannot be a symbolic link")
        target = self._safe_directory(path)
        if not target.is_dir():
            raise ValueError("path is not a directory")
        directories = []
        truncated = False
        examined = 0
        try:
            with scandir_no_follow(target) as entries:
                for entry in entries:
                    examined += 1
                    if examined > 10000:
                        truncated = True
                        break
                    try:
                        metadata = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if (stat.S_ISDIR(metadata.st_mode) and not is_link_or_reparse(metadata)
                            and (not query or query.casefold() in entry.name.casefold())):
                        directories.append({"name": entry.name, "path": str(Path(entry.path))})
        except OSError as exc:
            raise PermissionError("directory cannot be listed") from exc
        directories.sort(key=lambda item: item["name"].casefold())
        page = directories[offset:offset + limit]
        more = offset + limit < len(directories)
        return {"path": str(target), "parent": str(target.parent) if target.parent != target else None,
                "roots": root_page, "directories": page,
                "next_offset": offset + limit if more else None,
                "truncated": truncated or more}

    def select_workspace(self, principal, path):
        if not self.config.filesystem_browser_enabled:
            raise PermissionError("full filesystem browser is disabled")
        if principal.get("node_id") != self.config.node_id:
            raise PermissionError("workspace selection audience mismatch")
        if not isinstance(path, str) or len(path) > 4096 or "\x00" in path:
            raise ValueError("invalid directory path")
        target = Path(path).expanduser()
        if not target.is_absolute():
            raise PermissionError("workspace must be an absolute directory without a symbolic-link root")
        target = self._safe_directory(path)
        if not target.is_dir():
            raise ValueError("workspace path is not a directory")
        with self._connection(write=True) as conn:
            self._require_active_principal(conn, principal)
            existing = conn.execute(
                "SELECT workspace_id FROM device_connection_workspace_selections "
                "WHERE node_id=? AND user_id=? AND authorization_epoch=? AND path=? "
                "ORDER BY created_at DESC LIMIT 1",
                (self.config.node_id, principal["user_id"], principal["authorization_epoch"], str(target)),
            ).fetchone()
            if existing:
                return {"workspace_id": existing[0]}
            count = conn.execute(
                "SELECT count(*) FROM device_connection_workspace_selections "
                "WHERE node_id=? AND user_id=? AND authorization_epoch=?",
                (self.config.node_id, principal["user_id"], principal["authorization_epoch"]),
            ).fetchone()[0]
            if count >= 100:
                raise ValueError("too many selected workspace directories")
            workspace_id = "selected-" + secrets.token_urlsafe(18).replace("-", "a").replace("_", "b")
            conn.execute("INSERT INTO device_connection_workspace_selections VALUES(?,?,?,?,?,?)",
                         (self.config.node_id, principal["user_id"], principal["authorization_epoch"],
                          workspace_id, str(target), self._clock()))
        return {"workspace_id": workspace_id}

    @staticmethod
    def _safe_directory(path):
        """Resolve an existing directory while refusing links/reparse points in its path."""
        target = Path(path).expanduser()
        if not target.is_absolute():
            raise PermissionError("directory path must be absolute")
        if os.name == "nt" and str(target).startswith("\\\\"):
            raise PermissionError("network paths are not part of the local filesystem browser")
        current = Path(target.anchor)
        for component in target.parts[1:]:
            current = current / component
            try:
                metadata = current.lstat()
            except OSError as exc:
                raise PermissionError("directory path is unavailable") from exc
            if is_link_or_reparse(metadata):
                raise PermissionError("symbolic links and reparse points cannot be browsed")
        resolved = target.resolve(strict=True)
        if not resolved.is_dir():
            raise ValueError("path is not a directory")
        return resolved

    def path_allowed(self, principal, path):
        try:
            target = Path(path)
            if not target.is_absolute():
                return False
            target = target.resolve()
            return any(target.is_relative_to(Path(root).resolve())
                       for root in self.workspace_roots(principal).values())
        except (OSError, RuntimeError, ValueError, KeyError, TypeError):
            return False


def is_loopback(client):
    try:
        return ipaddress.ip_address(client[0] if client else "").is_loopback
    except ValueError:
        return False
