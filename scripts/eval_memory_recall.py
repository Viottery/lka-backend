"""Cross-session memory injection probes using actual scoped SQLite recall.

Measures retrieval, not end-to-end QA accuracy or a production latency SLO.
Disabled/history-only baselines have no history in the *new* test session.
"""

from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path

from app.core.memory_context import MemoryContextProvider
from app.domains.memory import MemoryInput, MemoryService, MemorySourceInput


def evaluate():
    with tempfile.TemporaryDirectory(prefix="lka-recall-eval-") as directory:
        root = Path(directory)
        service = MemoryService(root / "memory.sqlite3")
        service.ensure_schema()
        a, b = root / "project-a", root / "project-b"
        projects = {"a": service.resolve_project(a), "b": service.resolve_project(b)}
        records = {}
        entries = [
            ("preference", "回答先给结论并附来源", "preference", None, None),
            ("ticket", "上海演出票价上限600元", "user_fact", None, None),
            ("old-ticket", "杭州演出票价上限900元", "user_fact", None, "2000-01-01T00:00:00+00:00"),
            ("a-decision", "项目甲数据库使用SQLite", "project_decision", "a", None),
            ("b-decision", "项目乙数据库使用PostgreSQL", "project_decision", "b", None),
            ("revoked", "上海演出必须购买900元座位", "user_fact", None, None),
        ]
        for key, text, kind, project, expiry in entries:
            source = service.register_source(MemorySourceInput(source_type="user_message", source_ref=key))
            record = service.create(MemoryInput(content=text, memory_type=kind, source_id=source,
                                                scope="project" if project else "global",
                                                project_id=projects.get(project), sensitivity="normal",
                                                expires_at=expiry, user_confirmed=True))
            records[key] = record.memory_id
            if key == "revoked":
                service.revoke_source(source)
        cases = [
            ("new-session-style", None, "帮我解释代码接口", {"preference"}),
            ("new-session-ticket", None, "上海演出票价上限是多少", {"preference", "ticket"}),
            ("project-a", str(a), "数据库选型", {"preference", "a-decision"}),
            ("project-b", str(b), "数据库选型", {"preference", "b-decision"}),
            ("expired-ticket", None, "杭州", {"preference"}),
            ("unrelated-task", None, "整理代码接口", {"preference"}),
        ]
        provider = MemoryContextProvider(service)
        rows = []
        tp = fp = fn = 0
        for name, workspace, query, expected_keys in cases:
            started = time.perf_counter()
            actual = {item["memory_id"] for item in provider("new-session-" + name, workspace, query)["items"]}
            expected = {records[key] for key in expected_keys}
            tp += len(actual & expected)
            fp += len(actual - expected)
            fn += len(expected - actual)
            rows.append({"id": name, "passed": actual == expected,
                         "missing_ids": sorted(expected - actual), "unexpected_ids": sorted(actual - expected),
                         "local_ms": round((time.perf_counter() - started) * 1000, 3)})
        return {"synthetic_only": True, "production_slo": False, "metric": "memory_injection_not_qa",
                "cases": len(rows), "passed": sum(row["passed"] for row in rows),
                "memory_enabled": {"precision": tp / (tp + fp) if tp + fp else None,
                                   "recall": tp / (tp + fn) if tp + fn else None,
                                   "false_injections": fp, "omissions": fn},
                "disabled": {"recall": 0.0}, "session_summary_only": {"recall": 0.0},
                "baseline_explanation": "New sessions have no previous-session history or summary; no model answers are scored.",
                "results": rows}


if __name__ == "__main__":
    print(json.dumps(evaluate(), ensure_ascii=False, indent=2))
