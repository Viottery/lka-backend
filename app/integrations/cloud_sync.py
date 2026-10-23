"""Bounded HTTPS node polling, with a separately materialized shared-state replica."""
from __future__ import annotations

import json
import os
import threading
from datetime import UTC, datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class CloudSyncError(ValueError):
    pass


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise CloudSyncError("cloud_redirect_rejected")


class CloudReplicaSynchronizer:
    """The local node initiates every connection; it never accepts remote commands."""

    def __init__(self, config, replica, outbox, *, transport=None, connections=None, memory_writes=None,
                 memory_replication=None):
        self.config, self.replica, self.outbox = config, replica, outbox
        self._transport = transport or self._request
        self.connections = connections
        self.memory_writes = memory_writes
        self.memory_replication = memory_replication
        self._stop = threading.Event()
        self._run_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._thread = None
        self._state = {"last_success_at": None, "last_error": None, "running": False}

    def _request(self, method: str, path: str, payload, token: str):
        base = self.config.hub_url or ""
        parsed = urlsplit(base)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment or parsed.path not in {"", "/"}):
            raise CloudSyncError("cloud_https_origin_required")
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode() if payload is not None else None
        request = Request(base.rstrip("/") + path, method=method, data=body, headers={
            "Authorization": "Bearer " + token, "Accept": "application/json",
            "Content-Type": "application/json",
        })
        # Tailnet requests must not send node credentials to an environment proxy.
        opener = build_opener(ProxyHandler({}), _NoRedirect())
        try:
            with opener.open(request, timeout=self.config.request_timeout_seconds) as response:
                raw = response.read(self.config.max_page_bytes + 1)
                if len(raw) > self.config.max_page_bytes:
                    raise CloudSyncError("cloud_response_too_large")
                try:
                    result = json.loads(raw)
                except (ValueError, UnicodeError) as exc:
                    raise CloudSyncError("cloud_invalid_response") from exc
                if not isinstance(result, dict):
                    raise CloudSyncError("cloud_invalid_response")
                return result
        except HTTPError as exc:
            exc.close()
            raise CloudSyncError(f"cloud_http_{exc.code}") from None
        except (URLError, TimeoutError, OSError) as exc:
            raise CloudSyncError("cloud_unavailable") from exc

    def run_once(self, *, max_pages: int = 8) -> dict:
        if type(max_pages) is not int or not 1 <= max_pages <= 100:
            raise ValueError("invalid maximum pages")
        if not self._run_lock.acquire(blocking=False):
            raise CloudSyncError("cloud_sync_already_running")
        try:
            token = os.environ.get(self.config.token_env, "")
            if not token or "\r" in token or "\n" in token:
                raise CloudSyncError("cloud_node_token_missing")
            with self._state_lock:
                self._state["running"] = True
            remote = self._transport("GET", "/node/sync/status", None, token)
            if remote.get("hub_id") != self.replica.status()["hub_id"]:
                raise CloudSyncError("cloud_hub_identity_mismatch")
            heartbeat = self._transport("POST", "/node/heartbeat", {}, token)
            node_id = heartbeat.get("node_id") or heartbeat.get("device_id")
            if not isinstance(node_id, str) or not node_id:
                raise CloudSyncError("cloud_invalid_heartbeat")
            if self.connections is not None and node_id != self.connections.config.node_id:
                raise CloudSyncError("cloud_node_identity_mismatch")
            for _ in range(max_pages):
                if self._stop.is_set():
                    break
                after = self.replica.status()["cursor"]
                query = urlencode({"after": after, "limit": self.config.page_size})
                page = self._transport("GET", "/node/sync/changes?" + query, None, token)
                if page.get("after") != after:
                    raise CloudSyncError("cloud_cursor_mismatch")
                self.replica.apply_page(page)
                if not page["has_more"]:
                    break
                if page["cursor"] <= after:
                    raise CloudSyncError("cloud_cursor_did_not_advance")
            # Install only hashes/metadata delivered on authenticated node transport.
            # Admission errors cannot starve shared reads; established local
            # sessions need no live hub round trip.
            if self.connections is not None:
                grants = self._transport("GET", "/node/connection-grants", None, token)
                self.connections.install_grants(grants.get("items", []),
                                                authorizations=grants.get("authorizations", []))
            if self.memory_replication is not None:
                self.memory_replication.flush_once(node_id=node_id)
            if self.memory_writes is not None:
                self.memory_writes.flush_once(node_id=node_id)
            # Proposal authorization/errors must never starve shared-state reads.
            # Keep failed proposals durable and retry them after the next pull.
            for item in self.outbox.pending():
                if self._stop.is_set():
                    break
                receipt = self._transport("POST", "/node/sync/proposals", item, token)
                if receipt.get("node_id") != node_id:
                    raise CloudSyncError("cloud_proposal_node_mismatch")
                self.outbox.acknowledge(item, receipt)
            with self._state_lock:
                self._state.update(last_success_at=datetime.now(UTC).isoformat(), last_error=None)
        except Exception as exc:
            # Store categories only, never server bodies, URLs, credentials or proposal text.
            with self._state_lock:
                self._state["last_error"] = str(exc) if isinstance(exc, CloudSyncError) else type(exc).__name__
            raise
        finally:
            with self._state_lock:
                self._state["running"] = False
            self._run_lock.release()
        return self.status()

    def status(self) -> dict:
        with self._state_lock:
            state = dict(self._state)
        result = {**state, "replica": self.replica.status(), "outbox": self.outbox.status()}
        if self.memory_replication is not None:
            result["memory_aggregation"] = self.memory_replication.status()
        return result

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="lka-cloud-sync", daemon=True)
        self._thread.start()

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception:  # noqa: BLE001, S110 - run_once persists failure categories.
                pass
            self._stop.wait(self.config.poll_seconds)

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.config.request_timeout_seconds + 1)
