"""Goal-only, budgeted real-model probes with durable private run artifacts.

Run explicitly with --remote. Each case has its own database/workspace. This
does not touch the running backend, start mail sync, or invoke coding experts.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sqlite3
import subprocess
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from app.core.config import Settings
from app.core.runtime import LocalKnowledgeAgentRuntime
from evals.lka_evals.fixtures import apply_setup
from evals.lka_evals.live_budget import LiveBudget, instrument_service

CASES = {
    "known_fact": {
        "goal": "这个项目目前的状态是什么？唯一标识是什么？",
        "files": {"README.md": "# Sample project\nStatus: amber\nUnique marker: citrus-642\n"},
        "facts": ["amber", "citrus-642"], "read_only": True,
    },
    "long_file_tail": {
        "goal": "检查 deployment.log：最后一次部署最终成功了吗？给出最后一次的版本与错误原因。不要把之前成功的部署当成最终结果。",
        "files": {"deployment.log": "2026-10-01 release v3.1 SUCCESS\n" +
                  "INFO heartbeat normal; deployment monitor active\n" * 1800 +
                  "2026-10-05 release v3.9 FAILED: migration checksum mismatch CHECK_783\n"},
        "facts": ["v3.9", "checksum", "CHECK_783"], "read_only": True,
    },
    "conflicting_files": {
        "goal": "这个项目的发布窗口最终定在什么时候？对照资料中的更新，告诉我现在有效的安排和仍需确认的部分。",
        "files": {
            "README.md": "Release notes are in docs/.\n",
            "docs/original.md": "2026-09-01: release scheduled 2026-10-08 09:00, region west.\n",
            "docs/update.md": "2026-10-03: supersedes original schedule. Release 2026-10-09 14:30, region east. Rollback owner remains unconfirmed.\n",
        }, "facts": ["2026-10-09", "14:30", "east"], "read_only": True,
    },
    "native_code_fix": {
        "goal": "这个小项目分页会漏数据。请找出问题并修复，验证分页正常且没有破坏边界行为。",
        "files": {
            "README.md": "# Page demo\nPure Python, no external dependencies. Tests: python -m unittest discover -s tests\n",
            "paging.py": "def page(items, offset, limit):\n    if offset < 0 or limit < 0:\n        raise ValueError('negative bounds')\n    return items[offset:limit]\n",
            "tests/test_paging.py": "import unittest\nfrom paging import page\n\nclass PagingTests(unittest.TestCase):\n    def test_offset(self):\n        self.assertEqual(page(list(range(10)), 4, 3), [4, 5, 6])\n    def test_empty(self):\n        self.assertEqual(page([1, 2], 0, 0), [])\n    def test_past_end(self):\n        self.assertEqual(page([1, 2], 9, 3), [])\n    def test_invalid(self):\n        with self.assertRaises(ValueError):\n            page([1], -1, 2)\n",
        }, "verify_command": ["python", "-m", "unittest", "discover", "-s", "tests"],
    },
    "bash_diagnosis": {
        "goal": "应用启动失败，帮我查清原因，给出日志与配置证据。只诊断，不修改任何文件。",
        "files": {
            "README.md": "Configuration: config/app.env. Startup output: logs/startup.log.\n",
            "config/app.env": "DB_PORT=5433\nSERVICE_PORT=8766\n",
            "logs/startup.log": "INFO application boot\nERROR database refused connection localhost:5433\nINFO database service listening localhost:5432\n",
        }, "facts": ["5433", "5432"], "read_only": True,
    },
    "mail_conflict": {
        "goal": "Thesis defense 最终时间和地点是什么？同时告诉我材料提交截止时间。按最新邮件核对，不要混用过时安排。",
        "files": {}, "setup": {"mail_fixtures": ["hard_mailbox"]},
        "facts": ["2026-08-24", "10:30", "Room C"], "read_only": True,
    },
    "web_official": {
        "goal": "核实 Python 3.13 的 free-threaded 模式是不是默认开启，以及扩展模块可能有什么兼容性限制。请查官方资料并引用来源，不要只根据搜索摘要回答。",
        "files": {}, "web": True, "facts": ["3.13"], "read_only": True,
    },
    "real_mail_overview": {
        "goal": "把这批本地邮件按发件人整理，每类给出数量和主要主题，先给出本地邮件总数。不要同步远程邮箱。如果只处理了一部分，请说清实际覆盖范围，不要假装完整。",
        "files": {}, "real_mail": True, "read_only": True,
    },
    "synthetic_mail_overview": {
        "goal": "把这批本地邮件按发件人整理，每类给出数量和主要主题，先给出本地邮件总数。不要同步远程邮箱。如果只处理了一部分，请说清实际覆盖范围，不要假装完整。",
        "files": {}, "synthetic_mail": True, "read_only": True,
    },
    "heldout_unknown_path": {
        "goal": "这份工作区里，生产环境当前生效的标识和区域是什么？不要把历史备份当成当前配置。",
        "files": {"README.md": "Current deployment metadata is stored under operations/live/.\n",
                  "operations/archive/environment.txt": "Superseded: region north; deployment cedar-111.\n",
                  "operations/live/environment.txt": "Effective: region south; deployment olive-912.\n"},
        "facts": ["south", "olive-912"], "read_only": True,
    },
    "heldout_log_middle": {
        "goal": "运维说日志结尾全是正常心跳，所以部署没问题。请核对事件记录，指出最新一次部署的真实结果、版本和原因。",
        "files": {"README.md": "Deployment event stream: logs/events.log\n",
                  "logs/events.log": "2026-10-01 release v4.0 SUCCESS\n" +
                  "INFO ordinary heartbeat\n" * 900 +
                  "2026-10-04 release v4.2 FAILED: signature validation rejected SIG_729\n" +
                  "INFO ordinary heartbeat\n" * 900},
        "facts": ["v4.2", "SIG_729"], "read_only": True,
    },
    "heldout_mutable_default": {
        "goal": "这个模块连续独立调用时会串数据。定位问题并修复，验证独立调用和显式传入容器的行为都正确。",
        "files": {
            "README.md": "# Tag demo\nPure Python. Tests: python -m unittest discover -s tests\n",
            "tags.py": "def tag(value, labels=[]):\n    labels.append(value)\n    return labels\n",
            "tests/test_tags.py": "import unittest\nfrom tags import tag\n\nclass Tests(unittest.TestCase):\n    def test_independent(self):\n        a=tag('a'); b=tag('b')\n        self.assertEqual(a,['a']); self.assertEqual(b,['b']); self.assertIsNot(a,b)\n    def test_explicit(self):\n        labels=['x']; self.assertIs(tag('y',labels),labels); self.assertEqual(labels,['x','y'])\n    def test_empty(self):\n        self.assertEqual(tag(''),[''])\n",
        }, "verify_command": ["python", "-m", "unittest", "discover", "-s", "tests"],
    },
    "web_search_release": {
        "goal": "联网搜索 uv 当前最新稳定版本，并核实该版本发布时间与两项发布变化。尽量用项目官方发布记录；如果搜索结果和页面不一致，说明以哪个为准。给出可追溯来源，控制篇幅。",
        "files": {}, "web": True, "search_required": True, "read_only": True,
        "facts": ["uv"],
    },
    "parallel_audit": {
        "goal": "请用多个 Agent 独立核查这个服务的发布安排、数据备份状况和启动错误，再综合给出能否上线的结论、证据和仍未解决的问题。不同核查可以并行，不要修改文件。",
        "files": {"README.md": "Independent evidence: docs/release.txt, backups/status.txt, logs/service.log and config/service.env.\n",
                  "docs/release.txt": "Final release window 2026-10-10 16:00 UTC. Supersedes 2026-10-08. Approval pending: owner Nora.\n",
                  "backups/status.txt": "Backup created 2026-10-04. Restore check FAILED: missing segment BACKUP_419. No verified recovery yet.\n",
                  "logs/service.log": "ERROR startup database connection refused at localhost:5444. Actual database listens on5432.\n",
                  "config/service.env": "DB_PORT=5444\n"},
        "facts": ["2026-10-10", "16:00", "Nora", "BACKUP_419", "5444", "5432"],
        "read_only": True, "children_required": 2,
    },
    "knowledge_policy": {
        "goal": "根据本地知识资料，生产发布需要哪些审批？紧急修复还有什么补充要求？请给出资料依据。",
        "files": {}, "setup": {"knowledge_fixtures": ["retrieval_benchmark"]},
        "facts": ["两名", "24", "复盘"], "read_only": True,
    },
    "knowledge_multihop": {
        "goal": "CobaltMoss 项目的紧急发布最终需要谁负责升级处理，以及至少几个人批准？请从本地资料核对，说明依据和不能确认的部分。",
        "files": {}, "setup": {"knowledge": [{"documents": [
            {"source": {"source_type": "local_document", "display_name": "project_registry", "sensitivity": "public", "remote_policy": "allow"},
             "title": "project_registry", "text": "CobaltMoss 项目的维护团队是 AX-17，适用的紧急发布例外政策编号 R9。旧负责人记录已废止。"},
            {"source": {"source_type": "local_document", "display_name": "team_directory", "sensitivity": "public", "remote_policy": "allow"},
             "title": "team_directory", "text": "AX-17 团队的升级处理负责人为 Mira Fox；BY-28 团队负责人为 Owen Gray。两者不得混用。"},
            {"source": {"source_type": "local_document", "display_name": "exception_rules", "sensitivity": "public", "remote_policy": "allow"},
             "title": "exception_rules", "text": "R9 例外要求两名不同值班操作员批准。R8 允许一名操作员批准。资料没有记录 CobaltMoss 当前实际批准人员。"},
            {"source": {"source_type": "local_document", "display_name": "unrelated_project", "sensitivity": "public", "remote_policy": "allow"},
             "title": "unrelated_project", "text": "QuartzElm 项目维护团队 BY-28，政策编号 R8。"},
        ]}]}, "facts": ["Mira Fox", "两名", "R9"], "read_only": True,
    },
    "mail_specialist_overview": {
        "goal": "请让邮件专家完整整理这批本地邮件，按发件人逐一列出数量和主要主题，报告总数及实际覆盖范围。不要同步远程邮箱。",
        "files": {}, "synthetic_mail": True, "read_only": True, "children_required": 1,
        "required_agent_id": "mail_expert",
    },
    "heldout_broken_test_runner": {
        "goal": "这个小项目的名称归一化会让同名项匹配失败。请定位并修复，运行项目已有测试验证，不要安装依赖，不要改写测试来绕过失败。若文档里的测试入口不可用，请利用本地已有能力完成验证。",
        "files": {
            "README.md": "# Name keys\nPure Python, no dependencies. Legacy test entry: python -m old_project_test_runner\nThe test source is in tests/. The old runner is no longer distributed.\n",
            "names.py": "def normalize(value):\n    return value.lower()\n",
            "tests/test_names.py": "import unittest\nfrom names import normalize\n\nclass Tests(unittest.TestCase):\n    def test_spaces(self):\n        self.assertEqual(normalize('  Acme  '), 'acme')\n    def test_unicode(self):\n        self.assertEqual(normalize('Straße'), normalize('STRASSE'))\n    def test_empty(self):\n        self.assertEqual(normalize('   '), '')\n    def test_no_mutation(self):\n        original=' X '; normalize(original); self.assertEqual(original,' X ')\n",
        }, "verify_command": ["python", "-m", "unittest", "discover", "-s", "tests"],
    },
}


class _SearchReservation:
    def __init__(self, budget):
        self.budget = budget

    def reserve(self):
        call = self.budget.reserve(kind="search", stage="web.search")
        # Charge a query at dispatch. Failure does not return the quota.
        self.budget.finish(call, None, status="dispatched")


def _hashes(workspace: Path) -> dict[str, str]:
    return {p.relative_to(workspace).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in workspace.rglob("*") if p.is_file()
            and not {"__pycache__", ".git"}.intersection(p.relative_to(workspace).parts)}


def _initialize_fixture_repo(workspace: Path) -> None:
    """Stop Git discovery at the fixture boundary, never at the user's repository."""
    template = workspace.parent / "empty-git-template"
    template.mkdir()
    subprocess.run(["git", "init", "--quiet", f"--template={template.resolve()}"],
                   cwd=workspace, check=True, capture_output=True)
    subprocess.run(["git", "add", "--all"], cwd=workspace, check=True, capture_output=True)
    subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgSign=false",
                    "-c", "user.name=LKA Quality Fixture", "-c", "user.email=fixture@example.test",
                    "commit", "--quiet", "--allow-empty", "-m", "isolated fixture baseline"],
                   cwd=workspace, check=True, capture_output=True)


def _child_metrics(child_events: dict[str, list[dict]]) -> dict:
    completed = [event for events in child_events.values() for event in events
                 if event.get("type") == "llm_completed"
                 or str(event.get("type", "")).endswith(".llm_completed")]
    audits = [event.get("payload", {}).get("audit_record") or event.get("payload", {}) for event in completed]
    return {
        "llm_calls": len(completed),
        "llm_total_duration_ms": sum(audit.get("duration_ms") or 0 for audit in audits),
        "input_tokens": sum(audit.get("input_token_count") or 0 for audit in audits),
        "output_tokens": sum(audit.get("output_token_count") or 0 for audit in audits),
        "calls_missing_usage": sum(audit.get("input_token_count") is None
                                   or audit.get("output_token_count") is None for audit in audits),
    }


def _import_readonly_mail_snapshot(runtime, source: Path, *, limit: int = 60) -> dict:
    """Read committed source rows, never open the production database for writes."""
    with sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN")
        rows = conn.execute("SELECT * FROM mail_messages ORDER BY received_at DESC, message_id LIMIT ?",
                            (limit,)).fetchall()
        account_rows = {row["account_id"]: dict(row) for row in conn.execute("SELECT * FROM mail_accounts")}
    from app.domains.mail import MailAccountInput, MailMessageInput
    for account_id in {row["account_id"] for row in rows}:
        account = account_rows[account_id]
        runtime.import_mail(account=MailAccountInput(provider=account["provider"],
            email_address=account["email_address"], display_name=account["display_name"]), messages=[
                MailMessageInput(external_id=row["external_id"], folder=row["folder"],
                    subject=row["subject"], sender=row["sender"], received_at=row["received_at"],
                    body_text=row["body_text"], to=json.loads(row["recipients"]), cc=json.loads(row["cc"]))
                for row in rows if row["account_id"] == account_id])
    return {"selected_count": len(rows), "sender_counts": dict(Counter(row["sender"] for row in rows)),
            "body_scope": "committed local snapshot; attachment contents not copied",
            "source_db": str(source.resolve())}


def _sender_coverage(answer: str, counts: dict[str, int]) -> dict:
    """Check enumerated identities and counts, not merely a mentioned total.

    Themes and source support still require trace/semantic review. Exact identity
    and an explicit count in the same row are necessary, not a quality score.
    """
    matched = {}
    for sender, count in counts.items():
        address = re.search(r"[\w.+-]+@[\w.-]+", sender)
        identity = address.group(0) if address else sender
        rows = [row for row in answer.splitlines() if identity.casefold() in row.casefold()]
        matched[sender] = any(re.search(rf"(?<!\d){count}(?!\d)", row) for row in rows)
    return {"expected_groups": len(counts), "matched_groups": sum(matched.values()),
            "covered_messages": sum(counts[s] for s in counts if matched[s]),
            "all_sender_counts_present": all(matched.values()), "matches": matched}


def _import_synthetic_mail_batch(runtime) -> dict:
    from app.domains.mail import MailAccountInput, MailMessageInput
    counts = [1] * 18 + [2] * 5 + [3] * 3 + [4, 5, 7, 7]
    topics = ["purchase receipts", "release notices", "meeting invitations", "travel reservations"]
    messages = [MailMessageInput(external_id=f"batch-{group}-{number}",
        sender=f"sender-{group:02}@example.test", received_at="2026-09-12T10:30:00Z",
        subject=f"{topics[group % len(topics)]} update {number}",
        body_text=f"Update {number} for {topics[group % len(topics)]}.")
        for group, count in enumerate(counts) for number in range(count)]
    runtime.import_mail(account=MailAccountInput(email_address="owner@example.test"), messages=messages)
    return {"selected_count": len(messages), "sender_counts": dict(Counter(m.sender for m in messages)),
            "body_scope": "synthetic 60-message batch; no private information"}


async def run_case(case_id: str, *, output: Path, budget: LiveBudget, planning: bool = False,
                   timeout: float = 180, mail_db: Path | None = None, protocol: str = "configured",
                   mail_expert: bool = False, full_retrieval: bool = False) -> dict:
    if protocol not in {"configured", "json", "native"}:
        raise ValueError("unsupported protocol experiment")
    case = CASES[case_id]
    if case.get("real_mail") and mail_db is None:
        raise ValueError("real mail requires an explicit --mail-db read-only source")
    root = output / (case_id + "_" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f"))
    workspace = root / "workspace"
    workspace.mkdir(parents=True)
    for name, content in case["files"].items():
        path = workspace / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    _initialize_fixture_repo(workspace)
    before = _hashes(workspace)
    settings = Settings(LKA_DATA_DIR=root / "data", LKA_WORKSPACE_ROOTS=str(workspace))
    config = settings.load_local_config()
    if protocol != "configured":
        # Explicit evaluation-only capability experiment, never infer capabilities
        # from a model name or mutate the user's persistent configuration.
        clients = [client.model_copy(update={"supports_json_mode": True,
                    "supports_function_calling": protocol == "native"})
                   for client in config.llm.client_configs()]
        config = config.model_copy(update={"llm": config.llm.model_copy(update={"clients": clients})})
    config = config.model_copy(update={
        "agent": config.agent.model_copy(update={"orchestrator": "langgraph",
            "checkpoint_backend": "sqlite", "multi_agent_planning_enabled": planning,
            "mail_expert_enabled": mail_expert,
            "codex_expert_enabled": False, "max_decision_steps": 12}),
        "mail": config.mail.model_copy(update={
            "outlook": config.mail.outlook.model_copy(update={"enabled": False}),
            "imap": config.mail.imap.model_copy(update={"enabled": False})}),
        "memory": config.memory.model_copy(update={"background_enabled": False}),
        "message_history": config.message_history.model_copy(update={"background_enabled": False}),
        "embedding": config.embedding.model_copy(update={
            "enabled": config.embedding.enabled if full_retrieval else False,
            "local_files_only": True}),
        "reranker": config.reranker.model_copy(update={
            "enabled": config.reranker.enabled if full_retrieval else False,
            "local_files_only": True}),
        # Writes are explicitly authorized inside this synthetic fixture only.
        "safety": config.safety.model_copy(update={"tool_review_mode": "skip"}),
    })
    initialization_started = time.perf_counter()
    with patch.object(Settings, "load_local_config", return_value=config):
        runtime = LocalKnowledgeAgentRuntime(settings)
    initialization_seconds = time.perf_counter() - initialization_started
    instrument_service(runtime.agent_llm_client, budget, allowed_model=config.llm.model)
    search_tool = runtime.tool_registry.get_tool_or_none("web.search")
    if search_tool:
        search_tool.adapter.quota = _SearchReservation(budget)
    apply_setup(runtime, case.get("setup"))
    mail_snapshot = _import_readonly_mail_snapshot(runtime, mail_db) if case.get("real_mail") else None
    if case.get("synthetic_mail"):
        mail_snapshot = _import_synthetic_mail_batch(runtime)
    index_result = None
    index_error = None
    index_seconds = 0.0
    if full_retrieval:
        index_started = time.perf_counter()
        try:
            index_result = await asyncio.to_thread(runtime.sync_knowledge_semantic_index,
                                                   allow_model_download=False)
        except Exception as exc:  # noqa: BLE001 - retain preparation failures and fallback evidence
            index_error = {"type": type(exc).__name__, "message": str(exc)}
        index_seconds = time.perf_counter() - index_started
    session = runtime.create_session(title="quality: " + case_id)
    session_id = session.session.session_id
    runtime.set_session_workspace(session_id=session_id, path=str(workspace.resolve()), platform="linux")
    evaluation_run = runtime.create_agent_run(session_id=session_id, user_input=case["goal"])
    started = time.perf_counter()
    started_at = datetime.now(UTC)
    report = {"case_id": case_id, "goal": case["goal"], "model": config.llm.model,
              "planning": planning, "protocol": protocol, "session_id": session_id, "private_artifacts": str(root),
              "mail_expert_enabled": mail_expert,
              "retrieval_config": {"embedding_enabled": config.embedding.enabled,
                                   "reranker_enabled": config.reranker.enabled},
              "runtime_initialization_seconds": round(initialization_seconds, 3),
              "semantic_index_seconds": round(index_seconds, 3),
              "semantic_index_result": index_result.model_dump(mode="json") if index_result else None,
              "semantic_index_error": index_error,
              "evaluation_run_id": evaluation_run.run_id,
              "budget_before": budget.snapshot(), "test_output_cap_if_unspecified": 16384,
              "mail_snapshot": mail_snapshot, "started_at": started_at.isoformat(),
              "code_hashes": {p: hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in
                ["app/core/agent_turn.py", "app/core/tool_result_gate.py", "app/core/sessions.py",
                 "app/integrations/web_search.py"]}}
    try:
        result = await asyncio.wait_for(runtime.run_agent_turn_async(
            session_id=session_id, user_input=case["goal"], existing_run_id=evaluation_run.run_id), timeout=timeout)
        report["result"] = result.model_dump(mode="json")
        answer = result.answer
        parent = runtime.agent_run_manager.get_run(result.run_id)
        children = [child for child_id in (parent.child_run_ids if parent else ())
                    if (child := runtime.agent_run_manager.get_run(child_id)) is not None]
        report["child_runs"] = [child.model_dump(mode="json") for child in children]
        report["child_events"] = {child.run_id: [event.model_dump(mode="json") for event in
            runtime.agent_run_manager.list_events(child.run_id)] for child in children}
        report["child_metrics"] = _child_metrics(report["child_events"])
        checks = {"fact_coverage": all(s.lower() in answer.lower() for s in case.get("facts", []))}
        if case.get("children_required"):
            checks["required_children_executed"] = len(children) >= case["children_required"]
        if case.get("required_agent_id"):
            checks["required_specialist_executed"] = any(
                child.metadata.get("agent_id") == case["required_agent_id"] for child in children)
        if mail_snapshot:
            checks["snapshot_total_mentioned"] = str(mail_snapshot["selected_count"]) in answer
            report["sender_coverage"] = _sender_coverage(answer, mail_snapshot["sender_counts"])
            checks["all_sender_counts_present"] = report["sender_coverage"]["all_sender_counts_present"]
        if case.get("read_only"):
            checks["files_unchanged"] = _hashes(workspace) == before
        if case.get("verify_command"):
            completed = await asyncio.to_thread(subprocess.run, case["verify_command"],
                cwd=workspace, capture_output=True, text=True, timeout=15, check=False)
            report["independent_verification"] = {"exit_code": completed.returncode,
                "stdout": completed.stdout[-8000:], "stderr": completed.stderr[-8000:]}
            checks["independent_tests_pass"] = completed.returncode == 0
            checks["tests_not_modified"] = all(_hashes(workspace).get(p) == h
                for p, h in before.items() if p.startswith("tests/"))
        if case.get("web"):
            checks["page_evidence_loaded"] = any(e.tool_name == "web.open" and e.result.get("status") == "completed"
                                                for e in result.tool_events)
        if case.get("search_required"):
            checks["search_executed"] = any(e.tool_name == "web.search" and e.result.get("status") == "completed"
                                             for e in result.tool_events)
        # Coverage and a successful fetch are necessary, not semantic-proof scores.
        report["checks"] = checks
        report["mechanical_pass"] = all(checks.values())
        first_progress = next((event for event in result.progress_events
                               if event.type == "assistant_message"), None)
        report["metrics"] = {"first_agent_progress_seconds":
            round((datetime.fromisoformat(first_progress.created_at) - started_at).total_seconds(), 3)
            if first_progress else None,
            "first_final_token_seconds": None, "response_mode": "text",
            "llm_calls": len(result.llm_events),
            "llm_total_duration_ms": sum(event.duration_ms or 0 for event in result.llm_events),
            "tool_calls": len(result.tool_events),
            "failed_tools": sum(event.result.get("status") != "completed" for event in result.tool_events),
            "nonzero_command_exits": sum(
                isinstance(event.result.get("output"), dict)
                and type(event.result["output"].get("exit_code")) is int
                and event.result["output"]["exit_code"] != 0 for event in result.tool_events),
            "input_tokens": sum(event.input_token_count or 0 for event in result.llm_events),
            "output_tokens": sum(event.output_token_count or 0 for event in result.llm_events)}
        report["total_agent_metrics"] = {key: report["metrics"][key] + report["child_metrics"][key]
            for key in ("llm_calls", "llm_total_duration_ms", "input_tokens", "output_tokens")}
    except Exception as exc:  # noqa: BLE001 - preserve per-case failure evidence
        report["error"] = {"type": type(exc).__name__, "message": str(exc)}
        report["mechanical_pass"] = False
        if isinstance(exc, TimeoutError):
            runtime.agent_run_manager.cancel_run(evaluation_run.run_id, reason="isolated evaluation timeout")
    finally:
        persisted = runtime.agent_run_manager.get_run(evaluation_run.run_id)
        report["run_snapshot"] = persisted.model_dump(mode="json") if persisted else None
        report["run_events"] = [event.model_dump(mode="json") for event in
                                runtime.agent_run_manager.list_events(evaluation_run.run_id)]
        if persisted and "child_runs" not in report:
            children = [child for child_id in persisted.child_run_ids
                        if (child := runtime.agent_run_manager.get_run(child_id)) is not None]
            report["child_runs"] = [child.model_dump(mode="json") for child in children]
            report["child_events"] = {child.run_id: [event.model_dump(mode="json") for event in
                runtime.agent_run_manager.list_events(child.run_id)] for child in children}
            report["child_metrics"] = _child_metrics(report["child_events"])
        report["task_wall_seconds"] = round(time.perf_counter() - started, 3)
        report["budget_after"] = budget.snapshot()
        (root / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        runtime.stop()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--remote", action="store_true")
    parser.add_argument("--case", action="append", choices=sorted(CASES))
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--planning", action="store_true")
    parser.add_argument("--mail-expert", action="store_true", help="isolated opt-in to the existing mail specialist")
    parser.add_argument("--full-retrieval", action="store_true",
                        help="use configured cached local embedding/reranker; never download models")
    parser.add_argument("--protocol", choices=["configured", "json", "native"], default="configured",
                        help="isolated capability experiment; does not update persistent config")
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--mail-db", type=Path, help="explicit read-only production mail source for snapshot test")
    parser.add_argument("--output", type=Path, default=Path("data/quality_runs/linux_20261005"))
    args = parser.parse_args()
    if args.list:
        for name in CASES:
            print(name)
        return
    if not args.remote or not args.case:
        parser.error("explicit --remote and --case are required; no implicit paid full-suite run")
    output = args.output.resolve()
    budget = LiveBudget(output / "budget.sqlite3")
    for case_id in args.case:
        report = asyncio.run(run_case(case_id, output=output, budget=budget,
                                     planning=args.planning, timeout=args.timeout, mail_db=args.mail_db,
                                     protocol=args.protocol, mail_expert=args.mail_expert,
                                     full_retrieval=args.full_retrieval))
        print(json.dumps({k: report.get(k) for k in
              ("case_id", "task_wall_seconds", "mechanical_pass", "checks", "error", "private_artifacts",
               "budget_after")}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
