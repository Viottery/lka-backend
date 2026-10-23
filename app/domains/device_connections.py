"""One-use node admission grants and independent, locally persisted client sessions.

The hub never needs the node's credential plaintext. It sends grant hashes over
the existing authenticated node poll; only the client receives the grant secret.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import secrets
import sqlite3
import time
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.domains.workspace_mounts import WorkspaceMountConfig


class DeviceConnectionConfig(BaseModel):
    enabled: bool = False
    node_id: str = Field(default="", max_length=120)
    allowed_origins: list[str] = Field(default_factory=list, max_length=30)
    workspace_roots: dict[str, str] = Field(default_factory=dict, max_length=100)
    workspace_mounts: dict[str, WorkspaceMountConfig] = Field(default_factory=dict, max_length=100)
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
                    session_id TEXT PRIMARY KEY, workspace_ids TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS device_connection_authorizations (
                    node_id TEXT NOT NULL, user_id TEXT NOT NULL,
                    authorization_epoch INTEGER NOT NULL, PRIMARY KEY(node_id,user_id));
                CREATE TABLE IF NOT EXISTS device_connection_grant_epochs (
                    grant_id TEXT PRIMARY KEY, authorization_epoch INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS device_connection_session_epochs (
                    session_id TEXT PRIMARY KEY, authorization_epoch INTEGER NOT NULL);
            """)

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
        """Freeze server-owned tool admission; repeat clients can only narrow it."""
        self._enabled(node=True)
        if principal.get("node_id") != self.config.node_id:
            raise PermissionError("connection policy audience mismatch")
        selected = set(principal["workspace_ids"])
        if selected - self.config.workspace_roots.keys():
            raise PermissionError("connection policy workspace is not mapped locally")
        with self._connection(write=True) as conn:
            previous = conn.execute("SELECT workspace_ids FROM device_connection_session_policies "
                                    "WHERE session_id=?", (session_id,)).fetchone()
            if previous:
                selected &= set(json.loads(previous[0]))
            conn.execute("INSERT INTO device_connection_session_policies VALUES(?,?) "
                         "ON CONFLICT(session_id) DO UPDATE SET workspace_ids=excluded.workspace_ids",
                         (session_id, json.dumps(sorted(selected))))
        return self.session_policy(session_id)

    def session_policy(self, session_id):
        with self._connection() as conn:
            row = conn.execute("SELECT workspace_ids FROM device_connection_session_policies "
                               "WHERE session_id=?", (session_id,)).fetchone()
        if row is None:
            return None
        roots = [str(Path(self.config.workspace_roots[identity]).resolve())
                 for identity in json.loads(row[0]) if identity in self.config.workspace_roots]
        return {"allow_default_workspace": False, "allowed_paths": sorted(set(roots)),
                "safety_review_mode": "manual"}

    def path_allowed(self, principal, path):
        try:
            target = Path(path)
            if not target.is_absolute():
                return False
            target = target.resolve()
            return any(target.is_relative_to(Path(self.config.workspace_roots[identity]).resolve())
                       for identity in principal["workspace_ids"])
        except (OSError, RuntimeError, ValueError, KeyError, TypeError):
            return False


def is_loopback(client):
    try:
        return ipaddress.ip_address(client[0] if client else "").is_loopback
    except ValueError:
        return False
