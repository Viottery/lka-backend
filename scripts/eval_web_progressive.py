"""Small offline A/B for web acquisition, continuation and first evidence delivery.

Synthetic pages + MockTransport + temporary SQLite only; no provider or LLM calls.
Latency includes an explicitly simulated HTTP delay, not real internet performance.
"""

from __future__ import annotations

import argparse
import json
import math
import tempfile
import time
from pathlib import Path
from statistics import median

import httpx

from app.core.prompt_tokens import PromptTokenCounter
from app.core.tools import ToolContext, ToolExecutor, ToolRegistry
from app.domains.web_cache import WebCacheService
from app.integrations.web_search import PublicPageFetcher
from app.storage.db import connect, init_db
from app.tool_packages.web import WebFindTool, WebOpenTool
from app.tool_packages.web_resources import WebResources

CASES = (
    ("technical_tail", "Export mode", "Only with explicit opt-in; disabled by default."),
    ("chinese_event_tail", "演出报名", "仅接受已确认资格者，时间尚未最终确定。"),
    ("unicode_tail", "Straße", "Available only for this version; other versions are unknown."),
)


def run_case(root: Path, name: str, query: str, condition: str, delay: float) -> dict:
    text = "<main><h1>Reference</h1>" + "<p>unrelated background.</p>" * 1500
    text += f"<h2>Details</h2><p>{query} is described here.</p><p>{condition}</p></main>"
    calls = [0]

    def transport(request):
        calls[0] += 1
        time.sleep(delay)
        return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"}, text=text)

    fetcher = PublicPageFetcher(transport=httpx.MockTransport(transport))
    url = "https://8.8.8.8/fixture"
    start = time.perf_counter()
    legacy = fetcher.open(url)
    old_hit = fetcher.find(url, query)["matches"][0]
    fetcher.open(url, offset=old_hit["snippet_start"], max_chars=1200)
    legacy_ms = (time.perf_counter() - start) * 1000
    legacy_calls = calls[0]
    calls[0] = 0

    db = root / f"{name}.sqlite3"
    init_db(db)
    with connect(db) as conn:
        conn.execute("INSERT INTO agent_sessions VALUES ('s','fixture','active','{}','now','now')")
        conn.execute("INSERT INTO agent_runs VALUES ('r','s','running',?, 'now','now')",
                     (json.dumps({"parent_run_id": None, "metadata": {}}),))
    resources = WebResources(WebCacheService(lambda: connect(db)))
    registry = ToolRegistry()
    registry.register_tool(WebOpenTool(fetcher, resources))
    registry.register_tool(WebFindTool(fetcher, resources))
    executor = ToolExecutor(registry)
    context = ToolContext(session_id="s", run_id="r")

    def call(identity, tool, inputs):
        result = executor.execute(invocation_id=identity, tool_name=tool, tool_input=inputs, context=context)
        if result.status != "completed" or executor.validate_output(tool_name=tool, result=result):
            raise AssertionError(result.model_dump())
        return result.output

    start = time.perf_counter()
    initial = call("open", "web.open", {"url": url, "query": query})
    hit = call("find", "web.find", {"snapshot_id": initial["snapshot_id"], "query": query})["matches"][0]
    call("read", "web.open", {"snapshot_id": initial["snapshot_id"], "offset": hit["snippet_start"], "max_chars": 1200})
    current_ms = (time.perf_counter() - start) * 1000
    read_times = []
    for index in range(20):
        start = time.perf_counter()
        call(f"cached-{index}", "web.open", {"snapshot_id": initial["snapshot_id"], "offset": hit["snippet_start"], "max_chars": 1200})
        read_times.append((time.perf_counter() - start) * 1000)
    counter = PromptTokenCounter()
    return {
        "case": name, "legacy_http": legacy_calls, "snapshot_http": calls[0],
        "legacy_workflow_ms": round(legacy_ms, 2), "snapshot_workflow_ms": round(current_ms, 2),
        "cache_read_median_ms": round(median(read_times), 2),
        "cache_read_p95_ms": round(sorted(read_times)[math.ceil(len(read_times) * .95) - 1], 2),
        "legacy_first_body_chars": len(legacy["text"]), "snapshot_first_body_chars": len(initial["text"]),
        "legacy_first_condition_visible": condition in legacy["text"],
        "snapshot_first_condition_visible": condition in json.dumps(initial, ensure_ascii=False),
        "legacy_result_token_upper_bound": counter.count_text(json.dumps(legacy, ensure_ascii=False)).count,
        "snapshot_result_token_upper_bound": counter.count_text(json.dumps(initial, ensure_ascii=False)).count,
        "token_count_method": "utf8_byte_upper_bound_not_provider_usage",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--http-delay-ms", type=float, default=30)
    args = parser.parse_args()
    if not 0 <= args.http_delay_ms <= 1000:
        parser.error("http-delay-ms must be 0..1000")
    with tempfile.TemporaryDirectory(prefix="lka-web-eval-") as directory:
        rows = [run_case(Path(directory), *case, args.http_delay_ms / 1000) for case in CASES]
    print(json.dumps({"mode": "offline_synthetic", "simulated_http_delay_ms": args.http_delay_ms,
        "remote_search_requests": 0, "llm_calls": 0, "semantic_accuracy": "not_measured",
        "cases": rows}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
