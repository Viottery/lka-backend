"""Device registry and connection selection; this module does not execute remote work."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
from collections.abc import Callable
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

BoundedIdentifier = Annotated[str, Field(min_length=1, max_length=200)]

Scope = Literal[
    "heartbeat", "sync.read", "sync.propose", "connections.poll",
    "execution.poll", "execution.run", "execution.result", "sync.memory.write", "sync.memory.aggregate",
]


class DeviceConfig(BaseModel):
    enabled: bool = False
    online_threshold_seconds: int = Field(default=90, ge=5, le=86400)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DeviceSystem(StrictModel):
    environment_id: str = Field(min_length=1, max_length=120, pattern=r"^[\w.-]+$")
    name: str = Field(default="", max_length=120)
    platform: str = Field(default="", max_length=80)
    workspace_ids: list[BoundedIdentifier] = Field(default_factory=list, max_length=100)


class DeviceEnrollment(StrictModel):
    device_id: str = Field(min_length=1, max_length=120, pattern=r"^[\w.-]+$")
    name: str = Field(min_length=1, max_length=120)
    platform: str = Field(min_length=1, max_length=80)
    environment_id: str | None = Field(default=None, min_length=1, max_length=120)
    systems: list[DeviceSystem] = Field(default_factory=list, max_length=30)
    capabilities: list[BoundedIdentifier] = Field(default_factory=list, max_length=100)
    workspace_ids: list[BoundedIdentifier] = Field(default_factory=list, max_length=100)
    scopes: list[Scope] = Field(default_factory=lambda: ["heartbeat"])
    advertised_url: str | None = None

    @field_validator("advertised_url")
    @classmethod
    def tailscale_url(cls, value):
        if value is None:
            return None
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        if (
            parsed.scheme != "https"
            or not re.fullmatch(r"[a-z0-9.-]+\.ts\.net", host)
            or parsed.username
            or parsed.password
            or parsed.port not in (None, 443)
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("advertised_url must be a Tailscale HTTPS origin")
        return value

    @model_validator(mode="after")
    def environments(self):
        if not self.systems and self.environment_id:
            self.systems = [
                DeviceSystem(environment_id=self.environment_id, platform=self.platform)
            ]
        if not self.systems:
            raise ValueError("at least one system is required")
        if len({s.environment_id for s in self.systems}) != len(self.systems):
            raise ValueError("duplicate environment_id")
        if any(set(s.workspace_ids) - set(self.workspace_ids) for s in self.systems):
            raise ValueError("system workspaces must be approved device workspaces")
        return self


class DeviceUpdate(DeviceEnrollment):
    expected_revision: int = Field(ge=1)


class DeviceSelection(StrictModel):
    mode: Literal["fixed", "auto"] = "auto"
    device_id: BoundedIdentifier | None = None
    allowed_device_ids: list[BoundedIdentifier] = Field(min_length=1, max_length=100)
    environment_id: BoundedIdentifier | None = None
    workspace_id: BoundedIdentifier | None = None
    capabilities: list[BoundedIdentifier] = Field(default_factory=list, max_length=100)
    expected_revision: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def fixed_target(self):
        if self.mode == "fixed" and (
            not self.device_id or self.device_id not in self.allowed_device_ids
        ):
            raise ValueError("fixed device must belong to allowed_device_ids")
        return self


class DeviceConflictError(ValueError):
    pass


class DeviceUnavailableError(ValueError):
    pass


class DeviceService:
    def __init__(self, conn_factory: Callable[[], sqlite3.Connection], config: DeviceConfig):
        self._conn_factory = conn_factory
        self.config = config
        self.remote_execution_enabled = False
        with self._connection() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS cloud_devices (
                    device_id TEXT PRIMARY KEY, profile TEXT NOT NULL, token_hash TEXT UNIQUE NOT NULL,
                    credential_version INTEGER NOT NULL, revoked INTEGER NOT NULL DEFAULT 0,
                    revision INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    last_seen TEXT, status TEXT NOT NULL DEFAULT 'unknown');
                CREATE TABLE IF NOT EXISTS cloud_device_selections (
                    user_id TEXT NOT NULL, selection_key TEXT NOT NULL, selection TEXT NOT NULL,
                    revision INTEGER NOT NULL, PRIMARY KEY(user_id, selection_key));
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

    def _enabled(self):
        if not self.config.enabled:
            raise PermissionError("device service disabled")

    @staticmethod
    def _now():
        return datetime.now(UTC).isoformat()

    @staticmethod
    def _hash(token):
        return hashlib.sha256(token.encode()).hexdigest()

    def _record(self, row):
        result = json.loads(row["profile"])
        result.update(
            {k: row[k] for k in ("revision", "created_at", "updated_at", "last_seen", "status")}
        )
        result["node_id"] = row["device_id"]
        result["revoked"] = bool(row["revoked"])
        result["online"] = bool(
            not row["revoked"]
            and row["last_seen"]
            and (datetime.now(UTC) - datetime.fromisoformat(row["last_seen"])).total_seconds()
            <= self.config.online_threshold_seconds
        )
        return result

    def enroll(self, payload: DeviceEnrollment | dict):
        self._enabled()
        payload = DeviceEnrollment.model_validate(payload)
        token, now = secrets.token_urlsafe(48), self._now()
        with self._connection(write=True) as conn:
            try:
                conn.execute(
                    "INSERT INTO cloud_devices(device_id,profile,token_hash,credential_version,revision,created_at,updated_at) VALUES(?,?,?,1,1,?,?)",
                    (payload.device_id, payload.model_dump_json(), self._hash(token), now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise DeviceConflictError("device already enrolled") from exc
        return {**self.get(payload.device_id), "token": token}

    def list(self, limit=100, offset=0):
        self._enabled()
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM cloud_devices ORDER BY device_id LIMIT ? OFFSET ?",
                (-1 if limit is None else max(1, min(limit, 500)), max(0, offset)),
            )
            return {"items": [self._record(row) for row in rows]}

    def get(self, device_id):
        self._enabled()
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM cloud_devices WHERE device_id=?", (device_id,)
            ).fetchone()
            if row is None:
                raise KeyError(device_id)
            return self._record(row)

    def update(self, device_id, payload: DeviceUpdate | dict):
        self._enabled()
        payload = DeviceUpdate.model_validate(payload)
        if payload.device_id != device_id:
            raise ValueError("device identity is immutable")
        data = payload.model_dump(exclude={"expected_revision"})
        with self._connection(write=True) as conn:
            self._check_revision(conn, device_id, payload.expected_revision)
            conn.execute(
                "UPDATE cloud_devices SET profile=?,revision=revision+1,updated_at=? WHERE device_id=?",
                (json.dumps(data), self._now(), device_id),
            )
        return self.get(device_id)

    def _check_revision(self, conn, device_id, revision):
        row = conn.execute("SELECT * FROM cloud_devices WHERE device_id=?", (device_id,)).fetchone()
        if row is None:
            raise KeyError(device_id)
        if row["revision"] != revision:
            raise DeviceConflictError("device revision conflict")
        return row

    def revoke(self, device_id, expected_revision):
        self._enabled()
        with self._connection(write=True) as conn:
            self._check_revision(conn, device_id, expected_revision)
            conn.execute(
                "UPDATE cloud_devices SET revoked=1,credential_version=credential_version+1,revision=revision+1,updated_at=? WHERE device_id=?",
                (self._now(), device_id),
            )
        return self.get(device_id)

    def rotate(self, device_id, expected_revision):
        self._enabled()
        token = secrets.token_urlsafe(48)
        with self._connection(write=True) as conn:
            self._check_revision(conn, device_id, expected_revision)
            conn.execute(
                "UPDATE cloud_devices SET token_hash=?,credential_version=credential_version+1,revision=revision+1,updated_at=? WHERE device_id=?",
                (self._hash(token), self._now(), device_id),
            )
        return {**self.get(device_id), "token": token}

    def authenticate(self, token: str):
        if not self.config.enabled or not token:
            return None
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM cloud_devices WHERE token_hash=? AND revoked=0", (self._hash(token),)
            ).fetchone()
            if row is None:
                return None
            return {**self._record(row), "credential_version": row["credential_version"]}

    def require_node(self, conn, principal: dict, scope: str | None = None):
        """Call inside the same write transaction as the node's operation."""
        self._enabled()
        row = conn.execute(
            "SELECT * FROM cloud_devices WHERE device_id=?", (principal.get("node_id"),)
        ).fetchone()
        if (
            row is None
            or row["revoked"]
            or row["credential_version"] != principal.get("credential_version")
        ):
            raise PermissionError("device credential is no longer active")
        profile = json.loads(row["profile"])
        if scope and scope not in profile["scopes"]:
            raise PermissionError("device scope denied")
        return profile

    def heartbeat(self, principal, *, status="online", environment_id=None):
        if status not in ("online", "busy", "idle", "degraded"):
            raise ValueError("unsupported node status")
        with self._connection(write=True) as conn:
            profile = self.require_node(conn, principal, "heartbeat")
            if environment_id and environment_id not in {
                s["environment_id"] for s in profile["systems"]
            }:
                raise PermissionError("environment not approved")
            conn.execute(
                "UPDATE cloud_devices SET last_seen=?,status=? WHERE device_id=?",
                (self._now(), status, principal["node_id"]),
            )
        return self.get(principal["node_id"])

    def save_selection(self, user_id, payload: DeviceSelection | dict, selection_key="default"):
        self._enabled()
        payload = DeviceSelection.model_validate(payload)
        if not user_id or not selection_key or len(selection_key) > 200:
            raise ValueError("selection owner/key required")
        with self._connection(write=True) as conn:
            row = conn.execute(
                "SELECT revision FROM cloud_device_selections WHERE user_id=? AND selection_key=?",
                (user_id, selection_key),
            ).fetchone()
            revision = row["revision"] if row else 0
            if payload.expected_revision != revision:
                raise DeviceConflictError("selection revision conflict")
            for device_id in payload.allowed_device_ids:
                device = conn.execute(
                    "SELECT * FROM cloud_devices WHERE device_id=?", (device_id,)
                ).fetchone()
                if device is None or device["revoked"]:
                    raise ValueError("allowed device is not active")
            conn.execute(
                "INSERT INTO cloud_device_selections VALUES(?,?,?,?) ON CONFLICT(user_id,selection_key) DO UPDATE SET selection=excluded.selection,revision=excluded.revision",
                (
                    user_id,
                    selection_key,
                    payload.model_dump_json(exclude={"expected_revision"}),
                    revision + 1,
                ),
            )
        return self.get_selection(user_id, selection_key)

    def get_selection(self, user_id, selection_key="default"):
        self._enabled()
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM cloud_device_selections WHERE user_id=? AND selection_key=?",
                (user_id, selection_key),
            ).fetchone()
            if row is None:
                raise KeyError("selection not found")
            return {
                **json.loads(row["selection"]),
                "revision": row["revision"],
                "selection_key": selection_key,
            }

    def resolve(self, user_id, selection_key="default"):
        selection = self.get_selection(user_id, selection_key)
        allowed = set(selection["allowed_device_ids"])
        for device in self.list(limit=None)["items"]:
            if (
                device["device_id"] not in allowed
                or not device["online"]
                or (selection["mode"] == "fixed" and device["device_id"] != selection["device_id"])
                or not set(selection["capabilities"]).issubset(device["capabilities"])
                or (
                    selection["workspace_id"]
                    and selection["workspace_id"] not in device["workspace_ids"]
                )
            ):
                continue
            for system in sorted(device["systems"], key=lambda s: s["environment_id"]):
                if (
                    selection["environment_id"]
                    and selection["environment_id"] != system["environment_id"]
                ):
                    continue
                if (
                    selection["workspace_id"]
                    and selection["workspace_id"] not in system["workspace_ids"]
                ):
                    continue
                return {
                    "selection": selection,
                    "node_id": device["node_id"],
                    "device_id": device["device_id"],
                    "system": system,
                    "environment_id": system["environment_id"],
                    "remote_execution_available": bool(self.remote_execution_enabled and {
                        "execution.poll", "execution.run", "execution.result",
                    }.issubset(device["scopes"])),
                }
        raise DeviceUnavailableError("no eligible online device for selected target")
