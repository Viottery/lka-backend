"""Local, non-blocking polling for explicitly created daily watches."""

from __future__ import annotations

import asyncio
import json
import threading
from datetime import UTC, datetime, time, timedelta
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from app.core.multi_agent import TaskResultStatus
from app.core.watch_execution import WatchExecutionAdapter
from app.domains.watch import BriefingInput, WatchService
from app.domains.watch_briefing import (
    is_complete_briefing_payload,
    normalize_briefing,
    reconcile_current_observations,
)


def _merge_previous_observations(
    briefings: list[dict[str, Any]],
    *,
    for_comparison: bool = False,
) -> dict[str, list[dict[str, Any]]]:
    """Keep prompt previews bounded; full comparison text never enters the goal."""
    latest: dict[str, dict[str, Any]] = {}
    for briefing in reversed(briefings):
        # Historical contradictory records must not reintroduce section-order last-wins.
        changed, unchanged, _ = reconcile_current_observations(
            briefing.get("changes", []), briefing.get("unchanged", []), latest
        )
        for category, items in (("changes", changed), ("unchanged", unchanged)):
            for item in items:
                if not isinstance(item, dict):
                    continue
                event_key = item.get("event_key")
                if not isinstance(event_key, str) or not event_key:
                    continue
                latest[event_key] = {
                    key: item[key]
                    for key in (
                        "event_key",
                        "event_id",
                        "subject_key",
                        "title",
                        "claim",
                        "current_observation",
                        "previous_observation",
                    )
                    if key in item
                } | {
                    "evidence_refs": item.get("evidence_refs", []),
                    "last_seen": briefing.get("created_at"),
                    "prior_section": category,
                }
    recent = sorted(
        latest.values(), key=lambda item: str(item.get("last_seen") or ""), reverse=True
    )[:30]
    # Reconciliation always uses full local values; only the prompt projection is shortened.
    recent = [{key: (value if for_comparison and key in {
        "claim", "current_observation", "previous_observation",
    } else value[:240]) if isinstance(value, str) else value
        for key, value in item.items()} for item in recent]
    for item in recent:
        item["evidence_refs"] = [str(ref)[:240] for ref in item["evidence_refs"][:3]]
    return {
        "changes": [item for item in recent if item.get("prior_section") == "changes"],
        "unchanged": [item for item in recent if item.get("prior_section") == "unchanged"],
    }


def latest_due_slot(watch: dict[str, Any], now: datetime) -> datetime | None:
    """Return only the newest elapsed local-calendar slot, not a backlog."""
    zone = ZoneInfo(watch["timezone"])
    local_now = now.astimezone(zone)
    wall_time = time.fromisoformat(watch["daily_time"])
    day = local_now.date()
    candidate = datetime.combine(day, wall_time, tzinfo=zone).astimezone(UTC)
    if candidate > now:
        candidate = datetime.combine(day - timedelta(days=1), wall_time, tzinfo=zone).astimezone(
            UTC
        )
    start = datetime.fromisoformat(watch["starts_at"]) if watch.get("starts_at") else None
    end = datetime.fromisoformat(watch["ends_at"]) if watch.get("ends_at") else None
    created = datetime.fromisoformat(watch["created_at"])
    if candidate < created or (start and candidate < start) or (end and candidate > end):
        return None
    return candidate


class WatchScheduler:
    """Two bounded workers; never executes an Agent on the FastAPI event loop."""

    def __init__(
        self,
        runtime: Any,
        service: WatchService,
        *,
        adapter: WatchExecutionAdapter | None = None,
        worker_count: int = 2,
        poll_seconds: float = 30.0,
    ) -> None:
        self.runtime = runtime
        self.service = service
        self.adapter = adapter or WatchExecutionAdapter(runtime=runtime)
        self.worker_count = worker_count
        self.poll_seconds = poll_seconds
        self.owner_prefix = f"watch_worker_{uuid4().hex}"
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._threads: list[threading.Thread] = []
        self._active_run_ids: set[str] = set()
        self._active_by_watch: dict[str, set[str]] = {}
        self._active_lock = threading.Lock()

    def start(self) -> None:
        if self._threads:
            return
        self._stop.clear()
        for index in range(self.worker_count):
            thread = threading.Thread(
                target=self._worker,
                args=(f"{self.owner_prefix}_{index}",),
                name=f"lka-watch-{index}",
                daemon=True,
            )
            thread.start()
            self._threads.append(thread)

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        with self._active_lock:
            run_ids = tuple(self._active_run_ids)
        for run_id in run_ids:
            try:
                self.runtime.agent_run_manager.cancel_run(
                    run_id, reason="Watch scheduler is stopping."
                )
            except (KeyError, ValueError):
                pass
        for thread in self._threads:
            thread.join(timeout=3)
        self._threads.clear()

    def enqueue_due(self, *, now: datetime | None = None) -> int:
        current = now or datetime.now(UTC)
        count = 0
        for watch in self.service.list(status="active", limit=500):
            slot = latest_due_slot(watch, current)
            if slot is not None:
                occurrence = self.service.create_occurrence(watch["watch_id"], slot)
                if occurrence["status"] == "pending":
                    count += 1
        return count

    def enqueue_now(self, watch_id: str) -> dict[str, Any]:
        watch = self.service.get(watch_id)
        if watch["status"] != "active":
            raise ValueError("Paused watches cannot run; resume the watch first.")
        slot = datetime.now(UTC).replace(second=0, microsecond=0)
        occurrence = self.service.create_occurrence(watch_id, slot)
        self._wake.set()
        return occurrence

    def cancel_watch(self, watch_id: str) -> None:
        with self._active_lock:
            run_ids = tuple(self._active_by_watch.get(watch_id, ()))
        for run_id in run_ids:
            self.runtime.agent_run_manager.cancel_run(
                run_id,
                reason="Watch paused or deleted.",
            )

    def run_one(self, *, owner: str, now: datetime | None = None) -> bool:
        occurrence = self.service.claim_occurrence(
            owner=owner,
            now=now,
            lease_for=timedelta(minutes=5),
        )
        if occurrence is None:
            return False
        asyncio.run(self._execute(occurrence, owner=owner))
        return True

    def _worker(self, owner: str) -> None:
        while not self._stop.is_set():
            try:
                self.enqueue_due()
                if not self.run_one(owner=owner):
                    self._wake.wait(self.poll_seconds)
                    self._wake.clear()
            except Exception:  # noqa: BLE001 - preserve worker after one bad watch.
                self._wake.wait(self.poll_seconds)
                self._wake.clear()

    async def _lease_heartbeat(self, occurrence_id: str, owner: str, parent_run_id: str) -> None:
        while True:
            await asyncio.sleep(60)
            renewed = await asyncio.to_thread(
                self.service.renew_lease,
                occurrence_id,
                owner=owner,
            )
            if not renewed:
                self.runtime.agent_run_manager.cancel_run(
                    parent_run_id,
                    reason="Watch occurrence lease was lost.",
                )
                return

    async def _execute(self, occurrence: dict[str, Any], *, owner: str) -> None:
        watch_id = occurrence["watch_id"]
        parent_run_id: str | None = None
        try:
            watch = self.service.get(watch_id)
            scope_version = int(occurrence.get("scope_version", watch["version"]))
            if not self.service.is_current_scope(watch_id, scope_version):
                raise ValueError("Watch authorization changed before execution.")
            session_id = occurrence["session_id"]
            self.runtime.session_service.ensure_session(
                session_id=session_id,
                title=f"Update: {watch['title']}",
                metadata={
                    "entrypoint": "watch",
                    "watch_id": watch_id,
                    "occurrence_id": occurrence["occurrence_id"],
                },
            )
            self.runtime.session_service.append_message(
                session_id=session_id,
                role="user",
                content=f"请跟进：{watch['goal']}",
                payload={
                    "entrypoint": "watch",
                    "watch_id": watch_id,
                    "occurrence_id": occurrence["occurrence_id"],
                },
                message_id=f"watch_request_{occurrence['occurrence_id']}",
            )
            scope = watch["scope"]
            source_ids = tuple(scope.get("source_ids", ()))
            account_ids = tuple(scope.get("account_ids", ()))
            workspace_paths = tuple(scope.get("workspace_paths", ()))
            web_enabled = bool(
                scope.get(
                    "web_enabled", "web" in watch["categories"] or "news" in watch["categories"]
                )
            )
            matter_enabled = bool(scope.get("matter_enabled", "matter" in watch["categories"]))
            knowledge_enabled = bool(
                scope.get("knowledge_enabled", "knowledge" in watch["categories"])
            )
            config = getattr(self.runtime, "local_app_config", None)
            if web_enabled and config is not None and not config.web_search.resolved_api_key():
                raise ValueError("Web search is unavailable: configure BRAVE_SEARCH_API_KEY.")
            if web_enabled and account_ids and not scope.get("allow_mixed_private_external", False):
                raise ValueError(
                    "A watch combining private account data and web search requires explicit "
                    "allow_mixed_private_external scope."
                )
            # Grant flags are explicit watch configuration, never LLM-generated.
            previous = [
                item
                for item in self.service.list_briefings(watch_id=watch_id, limit=2)
                if item["occurrence_id"] != occurrence["occurrence_id"]
            ]
            previous_briefing = _merge_previous_observations(previous)
            comparison_briefing = _merge_previous_observations(previous, for_comparison=True)
            prior = json.dumps(previous_briefing, ensure_ascii=False, separators=(",", ":"))
            guidance = (
                self.runtime.instruction_files.for_watch()
                if hasattr(self.runtime, "instruction_files")
                else None
            )
            guidance_text = (
                "Watch operational guidance summary (not permission authority):\n"
                f"{str(guidance['summary'])[:320]}\n"
                f"Guidance continuation: {guidance['next_offset']}; if not null, use "
                "instructions.search/read to inspect relevant remaining sections before concluding.\n"
                if guidance
                else ""
            )
            goal = (
                f"Follow up this watch: {watch['goal']}\n"
                f"Previous structured observations (bounded and possibly stale JSON): {prior}\n"
                f"{guidance_text}"
                "Only report verified observations with exact evidence IDs or source URLs. "
                "State uncertainty and do not send mail, purchase, or change external state. "
                f"Apply importance rules exactly: {watch['importance_rules']!r}. "
                "If those rules filter an observed change, put it under unchanged and identify "
                "that it was filtered by importance rules."
            )
            parent = self.runtime.agent_run_manager.create_run(
                session_id=session_id,
                user_input=goal,
                metadata={
                    "entrypoint": "watch",
                    "watch_id": watch_id,
                    "occurrence_id": occurrence["occurrence_id"],
                },
            )
            parent_run_id = parent.run_id
            with self._active_lock:
                self._active_run_ids.add(parent_run_id)
                self._active_by_watch.setdefault(watch_id, set()).add(parent_run_id)
            self.runtime.agent_run_manager.mark_running(parent_run_id)
            if not self.service.is_current_scope(watch_id, scope_version):
                self.runtime.agent_run_manager.cancel_run(
                    parent_run_id,
                    reason="Watch authorization changed before tool execution.",
                )
                raise ValueError("Watch authorization changed before tool execution.")
            heartbeat = asyncio.create_task(
                self._lease_heartbeat(occurrence["occurrence_id"], owner, parent_run_id),
            )
            try:
                result = await self.adapter.run_async(
                    parent_run_id=parent_run_id,
                    watch_id=watch_id,
                    goal=goal,
                    source_ids=source_ids,
                    account_ids=account_ids,
                    web_enabled=web_enabled,
                    matter_enabled=matter_enabled,
                    workspace_paths=workspace_paths,
                    knowledge_enabled=knowledge_enabled,
                    # The extractive summary may omit rules even in a short file.
                    instruction_tools_enabled=bool(guidance),
                )
            finally:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
            budget_partial = (
                result.status == TaskResultStatus.PARTIAL
                and result.missing_requirements == ("child_budget_finish",)
                and result.failure_category is None
                and is_complete_briefing_payload(result.summary)
            )
            if result.status != TaskResultStatus.COMPLETED and not budget_partial:
                raise RuntimeError(result.failure_category or result.status.value)
            evidence = []
            for index, (ref, source) in enumerate(
                zip(result.evidence_refs, result.evidence_sources, strict=False)
            ):
                metadata = (
                    result.evidence_metadata[index] if index < len(result.evidence_metadata) else {}
                )
                evidence.append(
                    {
                        "kind": "source",
                        "ref": source,
                        "evidence_id": ref,
                        "observed_at": datetime.now(UTC).isoformat(),
                        **metadata,
                    }
                )
            normalized = normalize_briefing(
                summary=result.summary,
                evidence=evidence,
                previous=comparison_briefing,
                importance_rules=watch["importance_rules"],
                retrieval_failures=result.retrieval_failures,
                coverage_incomplete=budget_partial,
            )
            if budget_partial:
                self.runtime.agent_run_manager.append_event(
                    parent_run_id, "watch_partial_delivery",
                    "Deliver strictly normalized evidence with incomplete coverage; child remains partial.",
                    stage="watch", payload={"child_run_id": result.child_run_id,
                        "task_status": result.status.value,
                        "missing_requirements": result.missing_requirements},
                )
            persisted_evidence = [
                {key: value for key, value in item.items() if key != "excerpt"} for item in evidence
            ]
            briefing = self.service.complete_with_briefing(
                occurrence["occurrence_id"],
                owner=owner,
                run_id=parent_run_id,
                payload=BriefingInput(
                    title=watch["title"],
                    summary=normalized["summary"],
                    changes=normalized["changes"],
                    unchanged=normalized["unchanged"],
                    unconfirmed=normalized["unconfirmed"],
                    decisions=normalized["decisions"],
                    evidence=persisted_evidence,
                    content_fingerprint=normalized["fingerprint"],
                ),
            )
            self.runtime.session_service.append_message(
                session_id=session_id,
                role="agent",
                content=normalized["summary"],
                payload={
                    "entrypoint": "watch",
                    "watch_id": watch_id,
                    "briefing_id": briefing["briefing_id"],
                    "evidence": persisted_evidence,
                    "changes": normalized["changes"],
                    "unchanged": normalized["unchanged"],
                    "unconfirmed": normalized["unconfirmed"],
                    "decisions": normalized["decisions"],
                    "child_task_status": result.status.value,
                    "missing_requirements": list(result.missing_requirements),
                },
                message_id=f"watch_message_{occurrence['occurrence_id']}",
            )
            self.runtime.agent_run_manager.complete_run(
                parent_run_id,
                result_snapshot={"answer": normalized["summary"], "child_task_status": result.status.value},
            )
        except Exception as exc:  # noqa: BLE001 - persist one occurrence failure.
            if parent_run_id is not None:
                try:
                    self.runtime.agent_run_manager.fail_run(
                        parent_run_id,
                        error_type=type(exc).__name__,
                        error=str(exc),
                    )
                except (KeyError, ValueError):
                    pass
            try:
                self.service.finish_occurrence(
                    occurrence["occurrence_id"],
                    owner=owner,
                    succeeded=False,
                    run_id=parent_run_id,
                    error_category=type(exc).__name__,
                )
            except ValueError:
                pass
            try:
                self.runtime.session_service.append_message(
                    session_id=occurrence["session_id"],
                    role="agent",
                    content="本次定时关注检查未完成；请查看执行记录后重试。",
                    payload={
                        "entrypoint": "watch",
                        "watch_id": watch_id,
                        "occurrence_id": occurrence["occurrence_id"],
                        "status": "failed",
                        "error_category": type(exc).__name__,
                    },
                    message_id=f"watch_failure_{occurrence['occurrence_id']}",
                )
            except (KeyError, ValueError):
                pass
        finally:
            if parent_run_id is not None:
                with self._active_lock:
                    self._active_run_ids.discard(parent_run_id)
                    active = self._active_by_watch.get(watch_id)
                    if active is not None:
                        active.discard(parent_run_id)
                        if not active:
                            self._active_by_watch.pop(watch_id, None)
