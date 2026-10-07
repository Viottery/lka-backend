"""Shared collector orchestration and fresh, scoped MCP reads."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from pathlib import Path

from .backend import BackendClient, BackendUnavailable
from .config import ConnectorConfig
from .lease import SessionLease
from .models import CaptureBackpressure, MessageAdapter, SourceEvent
from .store import Store


@dataclass(frozen=True)
class AccessScope:
    # Set by the host configuration, never accepted from model-visible arguments.
    conversation_keys: frozenset[str] | None = None

    @property
    def allowed_keys(self) -> set[str] | None:
        return set(self.conversation_keys) if self.conversation_keys is not None else None


class Connector:
    def __init__(
        self,
        config: ConnectorConfig,
        *,
        backend: BackendClient | None = None,
        store: Store | None = None,
        scope: AccessScope | None = None,
    ):
        self.config = config
        config.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.backend = backend or BackendClient(config)
        self.store = store or Store(config.data_dir / "messages.sqlite3", config.max_pending)
        self.scope = scope or AccessScope()
        self.adapters: list[MessageAdapter] = []
        self._policy_lock = asyncio.Lock()
        self._media_lock = asyncio.Lock()
        self._media_cache = None
        self._state = {
            "capture": "stopped",
            "sync": "pending",
            "policy": "pending",
            "last_error": None,
            "capture_gaps": 0,
        }

    def register_adapter(self, adapter: MessageAdapter) -> None:
        if any(
            a.platform == adapter.platform and a.account_id == adapter.account_id
            for a in self.adapters
        ):
            raise ValueError("duplicate_adapter")
        self.adapters.append(adapter)

    async def refresh_policies(self) -> None:
        async with self._policy_lock:
            # Capture and MCP run as separate processes over this local store.
            # Serialize the FETCH and apply, including removed-policy snapshots,
            # so a delayed older response cannot restore a revoked permission.
            lease = await _policy_lease(self.config.data_dir / "policy.lock")
            try:
                policies = await self.backend.policies()
                media = await self.media_cache()

                def apply_snapshot():
                    media.reconcile(policies)
                    self.store.replace_policies(policies)

                await _thread_complete(apply_snapshot)
                self._state["policy"] = "current"
                self._state["policy_checked_at"] = int(time.time())
            finally:
                await asyncio.to_thread(lease.close)

    async def media_cache(self):
        async with self._media_lock:
            if self._media_cache is None:
                from .media import MediaCache

                self._media_cache = await asyncio.to_thread(MediaCache, self.config, self.store)
            return self._media_cache

    async def consume(self, adapter: MessageAdapter, event: SourceEvent) -> None:
        if (
            event.conversation.platform != adapter.platform
            or event.conversation.account_id != adapter.account_id
        ):
            raise PermissionError("adapter_identity_mismatch")
        lease = await _policy_lease(self.config.data_dir / "policy.lock")
        try:
            policy = await asyncio.to_thread(self.store.policy, event.conversation)
            if not policy or not policy.record_enabled:
                return
            message = adapter.normalize(event, policy)
            media = (
                await self.media_cache()
                if self.config.media_enabled and policy.media_enabled and message.attachments
                else None
            )

            def persist():
                # Persist while holding the cross-process permission lease. A
                # revoked media job cannot be inserted after its revoke fence.
                self.store.enqueue(message)
                if media is not None:
                    media.enqueue(message, policy)

            await _thread_complete(persist)
        finally:
            await asyncio.to_thread(lease.close)

    async def sync_once(self) -> None:
        await self.refresh_policies()
        rows = await asyncio.to_thread(self.store.pending)
        if rows:
            reply = await self.backend.import_messages(rows)
            await asyncio.to_thread(self.store.apply_ack, rows, reply)
        self._state["sync"] = "ok"
        self._state["last_sync_at"] = int(time.time())

    async def _periodic(self, action, interval: int, label: str):
        while True:
            try:
                await action()
            except (BackendUnavailable, ValueError, PermissionError, OSError):
                self._state[label] = "waiting"
                self._state["last_error"] = f"{label}_unavailable"
            await self._write_status()
            await asyncio.sleep(interval)

    async def _capture(self, adapter: MessageAdapter):
        while True:
            try:
                await adapter.connect()
                self._state["capture"] = "connected"
                await self._write_status()
                await adapter.capture(lambda event: self.consume(adapter, event))
                raise RuntimeError("source_disconnected")
            except CaptureBackpressure:
                self._state["capture"] = "backpressure"
                self._state["last_error"] = "outbox_full"
                self._state["capture_gaps"] += 1
            except PermissionError:
                # Authentication/account mismatch cannot be fixed by automatic
                # retries. Leave the session untouched and fail with a safe code.
                self._state["capture"] = "authentication_required"
                await self._write_status()
                raise
            except (RuntimeError, ValueError, OSError):
                self._state["capture"] = "reconnecting"
                self._state["last_error"] = "source_unavailable"
                self._state["capture_gaps"] += 1
            finally:
                await adapter.disconnect()
            await self._write_status()
            while (await asyncio.to_thread(self.store.status))[
                "pending"
            ] >= self.config.max_pending:
                await asyncio.sleep(self.config.sync_interval)
            await asyncio.sleep(5)

    async def run(self) -> None:
        # Backend unavailable on cold start is fine: cached approvals can support
        # offline capture. No approvals means no source message enters the store.
        try:
            await self.refresh_policies()
        except BackendUnavailable:
            self._state["policy"] = "cached"
        media = await self.media_cache()

        async def media_tick():
            await self.refresh_policies()
            await media.tick(self.adapters, self.backend)

        try:
            async with asyncio.TaskGroup() as group:
                group.create_task(self._periodic(self.sync_once, self.config.sync_interval, "sync"))
                group.create_task(
                    self._periodic(self.refresh_policies, self.config.policy_interval, "policy")
                )
                group.create_task(self._periodic(media_tick, 5, "media"))
                for adapter in self.adapters:
                    group.create_task(self._capture(adapter))
        finally:
            self._state["capture"] = "stopped"
            await self._write_status()
            await self.backend.close()

    async def backfill(self, adapter: MessageAdapter, conversation_key: str, limit: int) -> int:
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("invalid_backfill_limit")
        await self.refresh_policies()
        policies = await asyncio.to_thread(self.store.policies)
        policy = next((p for p in policies if p.key == conversation_key and p.record_enabled), None)
        if policy is None:
            raise PermissionError("conversation_unavailable")
        await adapter.connect()
        count = 0
        try:
            async for event in adapter.backfill(policy.conversation, limit):
                if count >= limit:
                    raise ValueError("adapter_backfill_limit_exceeded")
                # Permissions are refreshed on each bounded page/message. No
                # revoked history can pass through cached approvals here.
                await self.refresh_policies()
                await self.consume(adapter, event)
                count += 1
            # Drain bounded wire batches, including pre-existing backlog, without
            # deleting unacknowledged rows. A no-progress response ends draining.
            remaining = (await asyncio.to_thread(self.store.status))["pending"]
            for _ in range((remaining + 99) // 100 + 1):
                await self.sync_once()
                pending = (await asyncio.to_thread(self.store.status))["pending"]
                if pending == 0 or pending >= remaining:
                    break
                remaining = pending
            return count
        finally:
            await adapter.disconnect()

    async def query(self, method: str, *args, **kwargs) -> dict:
        if method not in {"conversations", "search", "history", "read_message", "context"}:
            raise ValueError("unknown_read_method")
        # Fail closed when the authoritative backend cannot confirm permission.
        # Offline capture and permission to disclose stored content are distinct.
        await self.refresh_policies()
        return await asyncio.to_thread(
            getattr(self.store, method), *args, allowed_keys=self.scope.allowed_keys, **kwargs
        )

    async def analysis(self, conversation_key: str, view: str, **kwargs) -> dict:
        await self.query("history", conversation_key, limit=1)
        return await self.backend.analysis(conversation_key, view, **kwargs)

    async def status(self) -> dict:
        status = await asyncio.to_thread(self.store.status)
        path = self.config.data_dir / "runtime_status.json"
        try:
            runtime = await asyncio.to_thread(_read_status, path)
        except (OSError, ValueError):
            runtime = {"capture": "unknown"}
        return {
            "storage": status,
            "runtime": runtime,
            "complete_for_platform": False,
            "coverage_note": "Only locally captured messages; gaps and earlier history may exist.",
        }

    async def _write_status(self):
        await asyncio.to_thread(
            _write_status,
            self.config.data_dir / "runtime_status.json",
            {**self._state, "updated_at": int(time.time())},
        )


def _write_status(path: Path, state: dict):
    import tempfile

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, delete=False
    ) as handle:
        handle.write(json.dumps(state))
        temp = Path(handle.name)
    try:
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def _read_status(path: Path):
    if path.stat().st_size > 4096:
        raise ValueError("invalid_status_file")
    return json.loads(path.read_text(encoding="utf-8"))


async def _policy_lease(path: Path):
    deadline = asyncio.get_running_loop().time() + 20
    while True:
        acquire = asyncio.create_task(asyncio.to_thread(SessionLease, path))
        try:
            return await asyncio.shield(acquire)
        except asyncio.CancelledError:
            # A cancelled thread acquisition must never strand an acquired lock.
            try:
                lease = await acquire
                await asyncio.to_thread(lease.close)
            except RuntimeError:
                pass
            raise
        except RuntimeError:
            if asyncio.get_running_loop().time() >= deadline:
                raise BackendUnavailable("policy_check_busy") from None
            await asyncio.sleep(0.05)


async def _thread_complete(function, *args):
    """Cancellation cannot release a permission lease before its worker commits."""
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await asyncio.gather(task, return_exceptions=True)
        raise
