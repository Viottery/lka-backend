"""Offline adversarial checks for progressive web evidence recovery.

Run with ``python -m scripts.eval_web_progressive_challenges``. All requests use
MockTransport and all cache state lives in a temporary SQLite database.
"""

from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

from app.core.agent_storage import SqliteAgentRunStore
from app.core.tool_result_gate import bounded_preview, needs_gate
from app.core.tools import ToolContext, ToolExecutor, ToolRegistry
from app.domains.web_cache import WebCacheService
from app.integrations.web_search import BraveSearchAdapter, PublicPageFetcher
from app.storage.db import connect, init_db
from app.tool_packages.web import WEB_PACKAGE, WebFindTool, WebOpenTool, WebSearchTool
from app.tool_packages.web_resources import WebResources

BASE = "https://8.8.8.8"


class OfflineRig:
    def __init__(self, db_path: Path, handler, *, clock=None, preview_chars=2400):
        self.db_path = db_path
        self.calls: list[str] = []

        def transport_handler(request: httpx.Request) -> httpx.Response:
            self.calls.append(str(request.url))
            return handler(request)

        transport = httpx.MockTransport(transport_handler)
        conn_factory = lambda: connect(db_path)
        self.cache = WebCacheService(conn_factory, ttl_seconds=60, clock=clock or time.time)
        self.resources = WebResources(self.cache, page_max_age_seconds=2,
                                      preview_chars=preview_chars, snippet_chars=80)
        self.fetcher = PublicPageFetcher(transport=transport)
        self.registry = ToolRegistry()
        self.registry.register_package(WEB_PACKAGE)
        self.registry.register_tool(WebOpenTool(self.fetcher, self.resources))
        self.registry.register_tool(WebFindTool(self.fetcher, self.resources))
        self.registry.register_tool(WebSearchTool(
            BraveSearchAdapter("offline-key", transport=transport), self.resources))
        self.executor = ToolExecutor(self.registry)
        self.context_index = 0

    def context(self) -> ToolContext:
        self.context_index += 1
        identity = uuid4().hex
        session = f"offline-session-{identity}"
        run = f"offline-run-{identity}"
        now = "2026-01-01T00:00:00+00:00"
        with connect(self.db_path) as conn:
            conn.execute("INSERT INTO agent_sessions VALUES (?,?,?,?,?,?)",
                         (session, "offline", "active", "{}", now, now))
            conn.execute("INSERT INTO agent_runs VALUES (?,?,?,?,?,?)",
                         (run, session, "running", '{"metadata":{}}', now, now))
        return ToolContext(session_id=session, run_id=run)

    def call(self, name: str, args: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        result = self.executor.execute(invocation_id=f"offline-{len(self.calls)}-{name}",
                                       tool_name=name, tool_input=args, context=context)
        return {"status": result.status, "output": result.output, "error": result.error,
                "raw_result": result.model_dump(mode="json")}


def html(body: str, *, status=200) -> httpx.Response:
    return httpx.Response(status, headers={"Content-Type": "text/html; charset=utf-8"},
                          text=f"<html><main>{body}</main></html>")


def record(name: str, expected: str, run, *, first=None, recovery=None,
           evidence=None, positions=None, gap="") -> dict[str, Any]:
    started = time.perf_counter()
    details = run()
    elapsed = round((time.perf_counter() - started) * 1000, 3)
    first_hit = bool(first(details)) if first else False
    recovery_hit = bool(recovery(details)) if recovery else False
    full = bool(details.get("full_pass", first_hit or recovery_hit))
    passed = full
    return {
        "case": name, "expected_capability": expected,
        "actual": {"key_evidence": evidence(details) if evidence else details.get("evidence", []),
                   "source_positions": positions(details) if positions else details.get("positions", []),
                   "http_count": details.get("http_count", 0), "latency_ms": elapsed},
        "full_pass": full, "first_view_hit": first_hit,
        "recovery_pass": recovery_hit, "passed": passed,
        "gap": details.get("gap", gap) if not passed else "",
        "first_view_limitation": details.get("gap", "") if passed and not first_hit else "",
    }


def main() -> int:
    reports = []
    with tempfile.TemporaryDirectory(prefix="lka-web-challenges-") as temp:
        db_path = Path(temp) / "offline.sqlite3"
        init_db(db_path)

        # 1. A distant disqualifier is recoverable through snapshot paging.
        body = "<h1>Eligibility</h1><p>Applicants may request the program.</p>" + "".join(
            f"<p>Background section {i} gives general information.</p>" for i in range(80))
        body += "<p>However, accounts under review are not eligible.</p>"
        def late_condition():
            rig = OfflineRig(db_path, lambda req: html(body))
            ctx = rig.context()
            view = rig.call("web.open", {"url": f"{BASE}/late", "query": "program"}, ctx)["output"]
            located = rig.call("web.find", {"snapshot_id": view["snapshot_id"],
                                "query": "under review"}, ctx)["output"]
            match = located["matches"][0]
            recover = rig.call("web.open", {"snapshot_id": view["snapshot_id"], "view": "page",
                               "offset": match["snippet_start"], "max_chars": 300}, ctx)["output"]
            found = "not eligible" in recover.get("text", "")
            return {"full_pass": found, "first": "not eligible" in view.get("text", ""),
                    "recovered": found, "evidence": [view.get("text", "")[:100], recover.get("text", "")],
                    "positions": [view.get("offset"), match["match_start"]], "http_count": len(rig.calls),
                    "gap": "late disqualifier was not in first view; snapshot page recovered it" if found else "snapshot page missed disqualifier"}
        reports.append(record("late_disqualifying_condition", "bounded recovery reaches distant condition",
            late_condition, first=lambda d: d["first"], recovery=lambda d: d["recovered"]))

        # 2. Generic high-frequency text should not hide a later specific restriction forever.
        repeated = "".join(f"<p>Access guidance: access may be requested. Section {i}.</p>" for i in range(45))
        repeated += "<p>Access is revoked for suspended members.</p>"
        def generic_distractor():
            rig = OfflineRig(db_path, lambda req: html(repeated))
            ctx = rig.context()
            first = rig.call("web.open", {"url": f"{BASE}/generic", "query": "access"}, ctx)["output"]
            find = rig.call("web.find", {"snapshot_id": first["snapshot_id"], "query": "revoked"}, ctx)["output"]
            recovered = any("suspended members" in m.get("snippet", "") for m in find.get("matches", []))
            return {"full_pass": recovered, "first": "suspended members" in first.get("text", ""),
                    "recovered": recovered, "evidence": [first.get("text", "")[:120],
                        *[m["snippet"] for m in find.get("matches", [])]],
                    "positions": [first.get("offset"), *[m["match_start"] for m in find.get("matches", [])]],
                    "http_count": len(rig.calls), "gap": "repeated generic sections dominated first view; literal find recovered restriction" if recovered else "restriction not recovered"}
        reports.append(record("generic_query_distractors", "specific late restriction remains recoverable",
            generic_distractor, first=lambda d: d["first"], recovery=lambda d: d["recovered"]))

        # 3. Natural Chinese query does not semantically expand a short English literal.
        def chinese_literal_gap():
            text = "Continue by entering your OTP in the verification field."
            rig = OfflineRig(db_path, lambda req: html("<p>Unrelated background.</p>" * 300
                + f"<p>{text}</p>"))
            ctx = rig.context()
            first = rig.call("web.open", {"url": f"{BASE}/zh", "query": "如何完成身份验证"}, ctx)["output"]
            miss = rig.call("web.find", {"snapshot_id": first["snapshot_id"], "query": "如何完成身份验证"}, ctx)["output"]
            recovery = rig.call("web.find", {"snapshot_id": first["snapshot_id"], "query": "OTP"}, ctx)["output"]
            honest_miss = first.get("query_status") == "no_lexical_match" and miss.get("match_status") == "no_literal_match"
            recovered = any("OTP" in row["snippet"] for row in recovery["matches"])
            return {"full_pass": honest_miss and recovered, "first": "OTP" in first.get("text", ""), "recovered": recovered,
                    "evidence": [first.get("query_status"), miss.get("match_status"),
                                 *[p.get("snippet", "") for p in miss.get("recovery_preview", [])]],
                    "positions": [p.get("snippet_start") for p in recovery.get("matches", [])],
                    "http_count": len(rig.calls), "gap": "Chinese query has no lexical match; caller-supplied OTP anchor recovers evidence locally, not automatic translation"}
        reports.append(record("chinese_query_short_literal", "honest lexical miss or useful explicit recovery",
            chinese_literal_gap, first=lambda d: d["first"], recovery=lambda d: d["recovered"]))

        # 4. A literal at the very end of a very long paragraph remains anchorable.
        long_text = "Ordinary policy wording. " * 1800 + " FINAL-CODE-9XZ"
        def paragraph_tail():
            rig = OfflineRig(db_path, lambda req: html(f"<p>{long_text}</p>"))
            ctx = rig.context()
            out = rig.call("web.open", {"url": f"{BASE}/tail", "query": "FINAL-CODE-9XZ"}, ctx)["output"]
            ok = "FINAL-CODE-9XZ" in out.get("text", "") and len(out.get("text", "")) <= 1200
            return {"full_pass": ok, "first": ok, "recovered": False, "evidence": [out.get("text", "")],
                    "positions": [out.get("offset"), long_text.rfind("FINAL-CODE-9XZ")],
                    "http_count": len(rig.calls), "gap": "late paragraph literal not selected within bounded view"}
        reports.append(record("long_paragraph_terminal_match", "query window anchors at final literal",
            paragraph_tail, first=lambda d: d["first"]))

        # 5. No literal phrase match exposes honest token previews, not a semantic claim.
        def no_match_recovery():
            text = "A permit may be required. Quantum review is handled separately."
            rig = OfflineRig(db_path, lambda req: html(f"<p>{text}</p>"))
            ctx = rig.context()
            out = rig.call("web.find", {"url": f"{BASE}/miss", "query": "quantum permit"}, ctx)["output"]
            previews = out.get("recovery_preview", [])
            tokens = {token for row in previews for token in row.get("query_tokens", [])}
            recovered = out.get("match_status") == "no_literal_match" and {"quantum", "permit"} <= tokens
            return {"full_pass": recovered, "first": False, "recovered": recovered,
                    "evidence": [out.get("match_status"), *[p.get("snippet", "") for p in previews]],
                    "positions": [p.get("snippet_start") for p in previews], "http_count": len(rig.calls),
                    "gap": "recovery previews should be reported as nonsemantic lexical hints"}
        reports.append(record("no_match_token_recovery", "no-match state and bounded token hints are explicit",
            no_match_recovery, recovery=lambda d: d["recovered"]))

        # 6. HTML table flattening can lose row/column boundaries; measure it directly.
        def table_association():
            table = "<table><tr><th>Region</th><th>Limit</th></tr><tr><td>North</td><td>12</td></tr><tr><td>South</td><td>4</td></tr></table>"
            rig = OfflineRig(db_path, lambda req: html(table))
            ctx = rig.context()
            out = rig.call("web.open", {"url": f"{BASE}/table"}, ctx)["output"]
            text = out.get("text", "")
            ok = "North" in text and "12" in text and "South" in text and "4" in text
            associated = "North 12" in text or "North\n12" in text
            return {"full_pass": ok and associated, "first": associated, "recovered": False,
                    "evidence": [text], "positions": [text.find("North"), text.find("12"), text.find("South"), text.find("4")],
                    "http_count": len(rig.calls), "gap": "values may be present while HTML table row/column association is flattened" if not associated else ""}
        reports.append(record("html_table_row_association", "readable extraction retains table row association",
            table_association, first=lambda d: d["first"]))

        # 7. Compact search is restorable from its local search snapshot, with Unicode intact.
        def search_restore():
            snippet = "中文🙂e\u0301证据" * 100
            def handler(req):
                if req.url.host == "api.search.brave.com":
                    return httpx.Response(200, json={"web": {"results": [{"title": "标题🙂", "url": f"{BASE}/result",
                        "description": snippet}]}})
                return html("<p>page</p>")
            rig = OfflineRig(db_path, handler)
            ctx = rig.context()
            compact = rig.call("web.search", {"query": "offline", "limit": 1}, ctx)["output"]
            restored = rig.call("web.search", {"search_id": compact.get("search_id"), "view": "full"}, ctx)["output"]
            row = restored.get("results", [{}])[0]
            ok = len(row.get("snippet", "")) == len(snippet) and row.get("snippet") == snippet
            return {"full_pass": ok, "first": len(compact.get("results", [{}])[0].get("snippet", "")) <= 80,
                    "recovered": ok, "evidence": [compact.get("search_id"), row.get("title"), row.get("snippet", "")[-24:]],
                    "positions": [len(compact.get("results", [{}])[0].get("snippet", "")), len(row.get("snippet", ""))],
                    "http_count": len(rig.calls), "gap": "compact result could not restore exact Unicode source snippet"}
        reports.append(record("unicode_search_restore", "compact rows retain local full-result restoration",
            search_restore, first=lambda d: d["first"], recovery=lambda d: d["recovered"]))

        # 8. Failed stale refresh must not corrupt an older immutable snapshot.
        def stale_refresh_failure():
            clock = [100.0]
            def handler(req):
                calls.append(str(req.url))
                if len(calls) > 1:
                    return httpx.Response(503)
                return html("<p>Cached version remains readable.</p>")
            calls = []
            rig = OfflineRig(db_path, handler, clock=lambda: clock[0])
            ctx = rig.context()
            first = rig.call("web.open", {"url": f"{BASE}/stale"}, ctx)["output"]
            clock[0] += 5
            failed = rig.call("web.open", {"url": f"{BASE}/stale"}, ctx)
            old = rig.call("web.open", {"snapshot_id": first["snapshot_id"]}, ctx)["output"]
            ok = failed["status"] == "failed" and "Cached version" in old.get("text", "")
            return {"full_pass": ok, "first": "Cached version" in first.get("text", ""),
                    "recovered": ok, "evidence": [failed.get("error"), old.get("text", "")],
                    "positions": [old.get("offset")], "http_count": len(rig.calls),
                    "gap": "failed refresh must leave the older snapshot readable"}
        reports.append(record("stale_refresh_failure", "refresh failure preserves prior immutable snapshot",
            stale_refresh_failure, first=lambda d: d["first"], recovery=lambda d: d["recovered"]))

        # 9. Reproduce canonical pending-result persistence followed by bounded preview.
        def persisted_gate_navigation():
            claim = "DISQUALIFYING-MIDDLE-CLAIM"
            body = "".join(f"<h2>Directory heading {i}</h2>" for i in range(100))
            body += "<p>Anchor " + "a" * 520 + " " + claim + " " + "b" * 250 + "</p>"
            rig = OfflineRig(db_path, lambda req: html(body))
            ctx = rig.context()
            page = rig.call("web.open", {"url": f"{BASE}/gate"}, ctx)["output"]
            result = rig.call("web.find", {"snapshot_id": page["snapshot_id"], "query": "Anchor"}, ctx)
            out, raw = result["output"], result["raw_result"]
            assert needs_gate(raw)
            store = SqliteAgentRunStore(db_path)
            store.put_artifact(artifact_id="tool_result_challenge_gate", run_id=ctx.run_id,
                kind="tool_result", payload=raw, summary="hard evidence delivery",
                created_at="2026-10-05T00:00:00+00:00")
            persisted = store.load_tool_result_artifact("tool_result_challenge_gate", ctx.run_id)
            gated = bounded_preview(persisted, max_string_chars=1200)["output"]
            matches_survive = "matches" in gated
            snapshot_survives = "snapshot_id" in gated
            claim_survives = claim in json.dumps(gated)
            ok = matches_survive and snapshot_survives and claim_survives
            return {"full_pass": ok, "first": matches_survive,
                    "recovered": snapshot_survives and claim_survives,
                    "evidence": ["sqlite_tool_result_roundtrip", list(gated.keys()),
                                 "claim_visible" if claim_survives else "claim_not_visible"],
                    "positions": [out.get("matches", [{}])[0].get("snippet_start"),
                                  out.get("matches", [{}])[0].get("snippet_end")],
                    "http_count": len(rig.calls),
                    "gap": "sorted persistence plus bounded preview dropped match navigation or middle claim"}
        reports.append(record("persisted_gate_evidence_navigation", "matches and snapshot survive pending-result persistence/preview",
            persisted_gate_navigation, first=lambda d: d["first"], recovery=lambda d: d["recovered"]))

    for item in reports:
        print(json.dumps(item, ensure_ascii=False, sort_keys=True))
    passed = sum(item["passed"] for item in reports)
    print(json.dumps({"summary": {"cases": len(reports), "passed": passed,
                                   "failed": len(reports) - passed,
                                   "first_view_hits": sum(row["first_view_hit"] for row in reports),
                                   "recovery_passes": sum(row["recovery_pass"] for row in reports),
                                   "llm_calls": 0, "real_network_requests": 0,
                                   "semantic_accuracy": "not_measured"}}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
