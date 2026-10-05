"""Production web tools: stable local continuation, authority, freshness and failure."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from itertools import count

import httpx
import pytest

from app.core.agent_graph import AgentGraphRunner
from app.core.agent_storage import SqliteAgentRunStore
from app.core.context_driver import ToolView
from app.core.multi_agent import SideEffectLevel
from app.core.tool_result_gate import bounded_preview
from app.core.tools import ToolContext, ToolExecutor, ToolRegistry
from app.domains.web_cache import WebCacheService
from app.integrations.web_search import BraveSearchAdapter, PublicPageFetcher
from app.storage.db import connect, init_db
from app.tool_packages.web import WEB_PACKAGE, WebFindTool, WebOpenTool, WebSearchTool
from app.tool_packages.web_resources import WebResources
from tests.test_answer_generation_recovery_quality import make_loop, response, scope

URL = "https://8.8.8.8/guide"


@pytest.fixture
def web(tmp_path):
    db = tmp_path / "web.sqlite3"
    init_db(db)
    now = [1000.0]
    cache = WebCacheService(lambda: connect(db), clock=lambda: now[0])
    resources = WebResources(cache)
    state = {"html": "<main><h1>Guide</h1><p>Original body.</p></main>", "status": 200}
    calls = []

    def transport(request):
        calls.append(str(request.url))
        if request.url.host == "api.search.brave.com":
            return httpx.Response(200, json={"web": {"results": [
                {"title": "Guide", "url": URL, "description": "search snippet " * 80,
                 "page_age": "2026-01-01", "page_fetched": "2026-01-02"},
            ]}})
        return httpx.Response(state["status"], headers={"content-type": "text/html; charset=utf-8"},
                              text=state["html"])

    fetcher = PublicPageFetcher(transport=httpx.MockTransport(transport))
    registry = ToolRegistry()
    registry.register_package(WEB_PACKAGE)
    registry.register_tool(WebOpenTool(fetcher, resources))
    registry.register_tool(WebFindTool(fetcher, resources))
    registry.register_tool(WebSearchTool(BraveSearchAdapter("synthetic-key", transport=httpx.MockTransport(transport)), resources))
    executor = ToolExecutor(registry)
    sequence = count()

    def context(session="s", run="r", child=False, view=None):
        with connect(db) as conn:
            conn.execute("INSERT OR IGNORE INTO agent_sessions VALUES (?,?,'active','{}','now','now')", (session, session))
            record = {"run_id": run, "session_id": session, "parent_run_id": "parent" if child else None,
                      "metadata": {}}
            conn.execute("INSERT OR IGNORE INTO agent_runs VALUES (?,?,'running',?,'now','now')",
                         (run, session, json.dumps(record)))
        if child and view is None:
            view = ToolView(snapshot_id=f"view-{run}", child_run_id=run,
                            allowed_packages=("web",), side_effect_level=SideEffectLevel.READ)
        return ToolContext(session_id=session, run_id=run, tool_view=view)

    def call(name, inputs, ctx=None):
        result = executor.execute(invocation_id=f"tool-{next(sequence)}", tool_name=name,
                                  tool_input=inputs, context=ctx or context())
        if result.status == "completed":
            assert executor.validate_output(tool_name=name, result=result) == []
        return result

    return type("WebFixture", (), {"db": db, "cache": cache, "resources": resources, "calls": calls,
        "state": state, "clock": now, "call": staticmethod(call), "context": staticmethod(context),
        "fetcher": fetcher, "registry": registry})()


def test_search_returns_compact_candidates_without_page_fetch_and_restores_original(web):
    first = web.call("web.search", {"query": "guide"}).output
    assert len(web.calls) == 1 and "brave.com" in web.calls[0]
    assert len(first["results"][0]["snippet"]) == 360
    assert first["results"][0]["snippet_truncated"] is True
    assert first["queried_at"] and first["cache_hit"] is False
    full = web.call("web.search", {"search_id": first["search_id"], "view": "full"}).output
    assert len(full["results"][0]["snippet"]) == 1000
    assert first["results"][0]["ref_id"] == full["results"][0]["ref_id"]
    assert len(web.calls) == 1 and full["cache_hit"] is True
    opened = web.call("web.open", {"ref_id": first["results"][0]["ref_id"]}).output
    assert opened["snapshot_stable"] is True and len(web.calls) == 2
    assert opened["fetched_at"] != first["results"][0]["published_at"]


def test_open_find_and_unicode_tail_reuse_one_immutable_snapshot(web):
    body = "正文🙂é\n\n" * 5000 + "<p>Target supports export.</p><p>Only when explicitly enabled.</p>"
    web.state["html"] = f"<main>{body}</main>"
    first = web.call("web.open", {"url": URL}).output
    assert len(first["text"]) <= 1200 and first["total_chars"] > 20_000
    identity = first["snapshot_id"]
    web.state["html"] = "<main>Changed at the remote source.</main>"
    hit = web.call("web.find", {"snapshot_id": identity, "query": "Target"}).output
    assert hit["matches"][0]["match_start"] > 20_000
    match = hit["matches"][0]
    assert "Only when explicitly enabled" in match["snippet"]
    tail = web.call("web.open", {"snapshot_id": identity, "offset": match["snippet_start"],
                                "max_chars": 1200, "expected_text_sha256": first["text_sha256"]}).output
    assert "Target" in tail["text"] and len(web.calls) == 1
    assert tail["text_sha256"] == first["text_sha256"]
    assert tail["cache_hit"] is True and tail["cache_read_at"] != tail["fetched_at"]


def test_query_overview_delivers_condition_not_just_the_page_prefix(web):
    web.state["html"] = "<main>" + "<p>irrelevant.</p>" * 3000 + "<h2>Export</h2><p>Target supports export.</p><p>Only when enabled.</p></main>"
    first = web.call("web.open", {"url": URL, "query": "Target"})
    output = first.output
    assert "Only when enabled" in output["text"] and output["offset"] > 20_000
    assert output["outline"][0]["text"] == "Export" and output["outline"][0]["heuristic"] is False
    preview = bounded_preview(first.model_dump(mode="json"), max_string_chars=1200)
    assert "Only when enabled" in json.dumps(preview)
    assert len(web.calls) == 1


def test_snapshot_excerpt_reaches_actual_answer_prompt(web, tmp_path):
    # Exercise production acquisition and the real context gate/provider boundary;
    # a scripted provider is not evidence of semantic answer accuracy.
    web.state["html"] = ("<main>" + "<p>irrelevant.</p>" * 3000
        + "<p>Target supports export. " + "Explanation. " * 60
        + "</p><p>Only when enabled.</p></main>")
    result = web.call("web.open", {"url": URL, "query": "Target"})
    loop, provider, manager = make_loop(tmp_path, [response("Evidence requires opt-in.")])
    loop.tool_executor = ToolExecutor(web.registry)
    store = SqliteAgentRunStore(tmp_path / "answer.sqlite3")
    loop.tool_invocation_store = store
    with scope(manager) as run:
        store.put_artifact(artifact_id=f"tool_result_{result.invocation_id}", run_id=run.run_id,
            kind="tool_result", payload=result.model_dump(mode="json"), summary="web excerpt",
            created_at=datetime.now(UTC).isoformat())
        observation = loop._observation_for_decision_prompt(tool_name=result.tool_name,
            tool_input={"url": URL, "query": "Target"}, tool_result=result,
            feedback={"status": "accepted", "protocol_status": "valid"}, run_id=run.run_id,
            force_gate=True)
        loop._answer_with_llm(user_input="What conditions apply to export?", route={},
            context_window={}, observations=[observation], final_decision=None, llm_events=[])
    delivered = provider.requests[0].messages[1].content
    assert "Only when enabled." in delivered and result.output["snapshot_id"] in delivered
    payload = json.loads(delivered)
    receipt = next(row for row in payload["context_delivery"] if row["path"] == "/output/text")
    assert receipt["coverage"] == "complete" and len(web.calls) == 1


def test_cache_size_failure_keeps_existing_and_expiry_cleanup_is_scoped(web):
    old = web.call("web.open", {"url": URL}).output
    web.cache.max_bytes = 10
    failed = web.call("web.open", {"url": URL, "refresh": True})
    assert failed.status == "failed" and "capacity" in failed.error
    assert web.call("web.open", {"snapshot_id": old["snapshot_id"]}).status == "completed"
    web.cache.max_bytes = 64_000_000
    web.clock[0] += 86401
    web.call("web.open", {"url": URL, "refresh": True})
    with connect(web.db) as conn:
        assert conn.execute("SELECT 1 FROM agent_run_artifacts WHERE artifact_id=?", (old["snapshot_id"],)).fetchone() is None
    assert web.cache.stats()["entries"] == 1


def test_refresh_creates_new_version_and_failure_preserves_old_snapshot(web):
    old = web.call("web.open", {"url": URL}).output
    web.state["html"] = "<main>Updated body.</main>"
    same = web.call("web.open", {"url": URL}).output
    assert same["snapshot_id"] == old["snapshot_id"] and len(web.calls) == 1
    new = web.call("web.open", {"url": URL, "refresh": True}).output
    assert new["snapshot_id"] != old["snapshot_id"] and len(web.calls) == 2
    assert new["text_sha256"] != old["text_sha256"]
    latest = web.call("web.open", {"url": URL}).output
    assert latest["snapshot_id"] == new["snapshot_id"]
    web.state["status"] = 403
    failed = web.call("web.open", {"url": URL, "refresh": True})
    assert failed.status == "failed" and "403" in failed.error
    original = web.call("web.open", {"snapshot_id": old["snapshot_id"]}).output
    assert original["text"] == old["text"] and len(web.calls) == 3


def test_explicit_zero_age_fetches_but_expired_fixed_reference_never_does(web):
    old = web.call("web.open", {"url": URL}).output
    web.call("web.open", {"url": URL, "max_age_seconds": 0})
    assert len(web.calls) == 2
    web.clock[0] += 400
    stale = web.call("web.open", {"snapshot_id": old["snapshot_id"], "max_age_seconds": 300})
    assert stale.status == "failed" and len(web.calls) == 2
    web.clock[0] += 86400
    expired = web.call("web.open", {"snapshot_id": old["snapshot_id"]})
    assert expired.status == "failed" and "expired" in expired.error and len(web.calls) == 2


def test_same_session_followup_and_restarted_service_can_read_without_network(web):
    old = web.call("web.open", {"url": URL}).output
    followup = web.call("web.open", {"snapshot_id": old["snapshot_id"]}, web.context(run="r2"))
    assert followup.status == "completed" and len(web.calls) == 1
    restarted = WebResources(WebCacheService(lambda: connect(web.db), clock=lambda: web.clock[0]))
    out = restarted.open(web.fetcher, {"snapshot_id": old["snapshot_id"]}, web.context(run="r3"))
    assert out["text"] == old["text"] and len(web.calls) == 1


def test_generic_observation_loader_cannot_read_web_artifact(web):
    old = web.call("web.open", {"url": URL}).output
    store = SqliteAgentRunStore(web.db)
    assert store.load_tool_result_artifact(old["snapshot_id"], "r") is None


def test_cross_session_and_child_references_cannot_expand_authority(web):
    root = web.call("web.open", {"url": URL}).output
    for ctx in (web.context("other", "other-r"), web.context(run="child1", child=True)):
        denied = web.call("web.open", {"snapshot_id": root["snapshot_id"]}, ctx)
        assert denied.status == "failed" and "unavailable" in denied.error
    child = web.context(run="child1", child=True)
    cached = web.call("web.open", {"url": URL}, child).output
    denied = web.call("web.open", {"snapshot_id": cached["snapshot_id"]}, web.context(run="child2", child=True))
    assert denied.status == "failed" and len(web.calls) == 2
    forged = child.model_copy(update={"tool_view": None})
    assert web.call("web.open", {"snapshot_id": cached["snapshot_id"]}, forged).status == "failed"


def test_deleted_session_cancelled_run_and_expired_view_cannot_read_cache(web):
    old = web.call("web.open", {"url": URL}).output
    with connect(web.db) as conn:
        conn.execute("UPDATE agent_runs SET status='cancelled' WHERE run_id='r'")
    assert web.call("web.open", {"snapshot_id": old["snapshot_id"]}).status == "failed"
    ctx = web.context(run="r2")
    with connect(web.db) as conn:
        conn.execute("UPDATE agent_sessions SET status='deleted' WHERE session_id='s'")
    assert web.call("web.open", {"snapshot_id": old["snapshot_id"]}, ctx).status == "failed"
    other = web.context("active", "active-r", view=ToolView(snapshot_id="expired", allowed_packages=("web",),
        side_effect_level=SideEffectLevel.READ, expires_at=datetime.now(UTC) - timedelta(seconds=1)))
    assert web.call("web.open", {"url": URL}, other).status == "rejected"
    assert len(web.calls) == 1


def test_current_permission_view_cannot_borrow_prior_unrestricted_reference(web):
    old = web.call("web.open", {"url": URL}).output
    restricted = web.context(run="r2", view=ToolView(snapshot_id="restricted", allowed_packages=("web",),
        side_effect_level=SideEffectLevel.READ))
    assert web.call("web.open", {"snapshot_id": old["snapshot_id"]}, restricted).status == "failed"
    revoked = restricted.model_copy(update={"tool_view": ToolView(snapshot_id="revoked",
        allowed_packages=("filesystem",), side_effect_level=SideEffectLevel.READ)})
    assert web.call("web.open", {"snapshot_id": old["snapshot_id"]}, revoked).status == "rejected"
    assert len(web.calls) == 1


@pytest.mark.parametrize("inputs", [
    {"url": URL, "expected_text_sha256": "z" * 64},
    {"url": URL, "snapshot_id": "invalid"}, {"url": URL, "offset": True},
    {"url": URL, "view": "unknown"}, {"url": URL, "query": " "},
    {"url": URL, "max_age_seconds": -1}, {"url": URL, "max_chars": 0}, {},
])
def test_malformed_requests_never_fetch(web, inputs):
    assert web.call("web.open", inputs).status in {"failed", "rejected"}
    assert web.calls == []


def test_hash_guard_on_cache_miss_does_not_refetch_and_wrong_hash_does_not_mix(web):
    assert web.call("web.open", {"url": URL, "offset": 1200,
        "expected_text_sha256": "0" * 64}).status == "failed"
    assert web.calls == []
    old = web.call("web.open", {"url": URL}).output
    bad = web.call("web.open", {"snapshot_id": old["snapshot_id"], "expected_text_sha256": "0" * 64})
    assert bad.status == "failed" and "changed" in bad.error and len(web.calls) == 1


def test_eviction_only_removes_web_artifacts_and_fixed_reference_is_terminal(web):
    web.cache.max_entries = 1
    old = web.call("web.open", {"url": URL}).output
    with connect(web.db) as conn:
        conn.execute("INSERT INTO agent_run_artifacts VALUES ('untouched','r','tool_result','hash','s','{}','now','now')")
    web.call("web.open", {"url": URL, "refresh": True})
    failed = web.call("web.open", {"snapshot_id": old["snapshot_id"]})
    assert failed.status == "failed" and len(web.calls) == 2
    with connect(web.db) as conn:
        assert conn.execute("SELECT 1 FROM agent_run_artifacts WHERE artifact_id='untouched'").fetchone()
    assert web.cache.stats()["entries"] == 1


def test_cancel_during_fetch_cannot_publish_snapshot(web, monkeypatch):
    old_read = web.fetcher.read

    def read_then_cancel(*args, **kwargs):
        value = old_read(*args, **kwargs)
        with connect(web.db) as conn:
            conn.execute("UPDATE agent_runs SET status='cancelled' WHERE run_id='r'")
        return value

    monkeypatch.setattr(web.fetcher, "read", read_then_cancel)
    assert web.call("web.open", {"url": URL}).status == "failed"
    assert web.cache.stats()["entries"] == 0 and web.cache.stats()["pending"] == 0


def test_content_integrity_failure_never_returns_forged_text(web):
    old = web.call("web.open", {"url": URL}).output
    with connect(web.db) as conn:
        conn.execute("UPDATE agent_run_artifacts SET payload='{}' WHERE artifact_id=?", (old["snapshot_id"],))
    result = web.call("web.open", {"snapshot_id": old["snapshot_id"]})
    assert result.status == "failed" and "integrity" in result.error and len(web.calls) == 1


def test_concurrent_same_page_acquisition_is_coalesced(web, monkeypatch):
    ctx = web.context()
    entered, release = threading.Event(), threading.Event()
    old_read = web.fetcher.read

    def delayed(*args, **kwargs):
        entered.set()
        assert release.wait(2)
        return old_read(*args, **kwargs)

    monkeypatch.setattr(web.fetcher, "read", delayed)
    with ThreadPoolExecutor(max_workers=3) as pool:
        first = pool.submit(web.resources.open, web.fetcher, {"url": URL}, ctx)
        assert entered.wait(2)
        second = pool.submit(web.resources.find, web.fetcher, {"url": URL, "query": "body"}, ctx)
        # Wait until the follower has reached the shared flight, not arbitrary network sleep.
        deadline = time.monotonic() + 2
        while web.cache.stats()["pending"] < 2 and time.monotonic() < deadline:
            threading.Event().wait(0.005)
        release.set()
        left, right = first.result(timeout=2), second.result(timeout=2)
    assert left["snapshot_id"] == right["snapshot_id"] and len(web.calls) == 1
    assert right["acquisition_shared"] is True and web.cache.stats()["pending"] == 0


def test_bounded_queue_and_wait_deadline_do_not_leak_capacity(web):
    cache = WebCacheService(lambda: connect(web.db), max_parallel=1, max_pending=2,
                            request_timeout_seconds=0.1)
    entered, release = threading.Event(), threading.Event()

    def held(deadline):
        entered.set()
        release.wait(1)
        return {"done": True}

    def run(key):
        return cache.acquire(scope_key="scope", key=key, operation=held, check=lambda: None)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(run, "a")
        assert entered.wait(1)
        second = pool.submit(run, "b")
        deadline = time.monotonic() + 1
        while cache.stats()["pending"] < 2 and time.monotonic() < deadline:
            threading.Event().wait(0.005)
        with pytest.raises(RuntimeError, match="queue is full"):
            run("c")
        with pytest.raises(RuntimeError, match="deadline"):
            second.result(timeout=1)
        release.set()
        with pytest.raises(RuntimeError, match="deadline"):
            first.result(timeout=1)
    assert cache.stats()["pending"] == 0
    assert cache.acquire(scope_key="s", key="ok", operation=lambda _: {"ok": True}, check=lambda: None) == {"ok": True}


def test_cancel_after_semaphore_acquisition_does_not_leak_permit(web):
    cache = WebCacheService(lambda: connect(web.db), max_parallel=1)
    checks = count()

    def cancel():
        if next(checks) >= 2:
            raise RuntimeError("cancelled")

    with pytest.raises(RuntimeError, match="cancelled"):
        cache.acquire(scope_key="s", key="bad", operation=lambda _: {}, check=cancel)
    assert cache.acquire(scope_key="s", key="ok", operation=lambda _: {"ok": True}, check=lambda: None)["ok"]


def test_independent_pages_use_bounded_parallelism(web, monkeypatch):
    ctx = web.context()
    web.cache._semaphore = threading.BoundedSemaphore(2)
    lock = threading.Lock()
    active, peak = [0], [0]
    entered, release = threading.Event(), threading.Event()
    old_read = web.fetcher.read

    def delayed(*args, **kwargs):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
            if active[0] == 2:
                entered.set()
        try:
            assert release.wait(2)
            return old_read(*args, **kwargs)
        finally:
            with lock:
                active[0] -= 1

    monkeypatch.setattr(web.fetcher, "read", delayed)
    with ThreadPoolExecutor(max_workers=4) as pool:
        jobs = [pool.submit(web.resources.open, web.fetcher, {"url": f"{URL}/{i}"}, ctx) for i in range(4)]
        assert entered.wait(2)
        release.set()
        assert all(job.result(timeout=3)["snapshot_id"] for job in jobs)
    assert peak[0] == 2 and len(web.calls) == 4 and web.cache.stats()["pending"] == 0


def test_async_graph_tool_boundary_keeps_event_loop_responsive(web, monkeypatch):
    ctx = web.context()
    entered, release = threading.Event(), threading.Event()
    old_read = web.fetcher.read

    def delayed(*args, **kwargs):
        entered.set()
        assert release.wait(2)
        return old_read(*args, **kwargs)

    monkeypatch.setattr(web.fetcher, "read", delayed)

    async def exercise():
        node = AgentGraphRunner._async_node(lambda _: web.call("web.open", {"url": URL}, ctx))
        task = asyncio.create_task(node({}))
        for _ in range(200):
            if entered.is_set():
                break
            await asyncio.sleep(0.005)
        assert entered.is_set() and not task.done()
        # Reaching this point before releasing the blocking transport proves the loop is live.
        release.set()
        result = await asyncio.wait_for(task, timeout=2)
        assert result.status == "completed"

    asyncio.run(exercise())
