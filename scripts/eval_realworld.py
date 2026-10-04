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
from urllib.parse import urlsplit, urlunsplit

from app.core.config import Settings
from app.core.llm import LLMResponseMode
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
    "heldout_knowledge_long_policy": {
        "goal": "请核对本地知识资料中的 Nebula 生产发布政策：常规发布和紧急修复分别需要多少人审批，紧急修复还有什么补充要求？不能只看前面的常规条款，请给出资料依据，简短回答。",
        "files": {}, "setup": {"knowledge": [{"documents": [{
            "source": {"source_type": "local_document", "display_name": "Nebula release policy",
                       "sensitivity": "public", "remote_policy": "allow"},
            "title": "Nebula release policy",
            "text": "Nebula 生产发布政策：常规发布需要两名维护者批准。\n" +
                    "归档规范说明：资料编号与归档时间应当保留，历史版本只用于审计，不是当前审批要求。" * 35 +
                    "\n紧急修复例外：允许一名值班维护者先批准，但须在18小时内补交事件复盘。",
        }]}]}, "facts": ["两名", "一名", "18"], "read_only": True,
    },
    "mail_specialist_overview": {
        "goal": "请让邮件专家完整整理这批本地邮件，按发件人逐一列出数量和主要主题，报告总数及实际覆盖范围。不要同步远程邮箱。",
        "files": {}, "synthetic_mail": True, "read_only": True, "children_required": 1,
        "required_agent_id": "mail_expert",
    },
    "real_mail_specialist_overview": {
        "goal": "请让邮件专家整理这批本地邮件，逐个发件人报告数量和主要主题，先报告本地总数和实际覆盖范围。不进行远程同步；标题推断与正文证据请区分。",
        "files": {}, "real_mail": True, "read_only": True, "children_required": 1,
        "required_agent_id": "mail_expert",
    },
    "mail_specialist_review": {
        "goal": "请邮件专家审阅全部本地邮件，整理需要我行动的事项和当前有效的截止时间；有更新冲突时以最新通知为准，缺失的负责人不要猜测。说明实际读取和分析覆盖范围，不同步远程邮箱。",
        "files": {}, "read_only": True, "children_required": 1, "required_agent_id": "mail_expert",
        "setup": {"mail": {"account": {"email_address": "review@example.test"}, "messages": [
            {"external_id": "old", "subject": "Project notification", "sender": "ops@example.test",
             "received_at": "2026-10-01T09:00:00Z",
             "body_text": "旧安排：Cobalt 项目材料截止 2026-10-08 09:00 UTC，负责人尚未确定。"},
            {"external_id": "new", "subject": "Project notification", "sender": "ops@example.test",
             "received_at": "2026-10-04T09:00:00Z",
             "body_text": "本通知取代旧安排：Cobalt 项目材料截止改为 2026-10-09 14:30 UTC，需要提交 budget.csv。负责人仍待确认。"},
            {"external_id": "security", "subject": "Service notification", "sender": "security@example.test",
             "received_at": "2026-10-04T12:00:00Z",
             "body_text": "需要在 2026-10-07 18:00 UTC 之前完成账号复核；工单 ACCOUNT-729，复核联系人 Mira。"},
            {"external_id": "receipt", "subject": "Service notification", "sender": "billing@example.test",
             "received_at": "2026-10-04T13:00:00Z", "body_text": "付款收据已确认，无需进一步操作。"},
        ]}}, "facts": ["2026-10-09", "14:30", "budget.csv", "ACCOUNT-729", "Mira"],
    },
    "heldout_broken_test_runner": {
        "goal": "这个小项目的名称归一化会让同名项匹配失败。请定位并修复，运行项目已有测试验证，不要安装依赖，不要改写测试来绕过失败。若文档里的测试入口不可用，请利用本地已有能力完成验证。",
        "files": {
            "README.md": "# Name keys\nPure Python, no dependencies. Legacy test entry: python -m old_project_test_runner\nThe test source is in tests/. The old runner is no longer distributed.\n",
            "names.py": "def normalize(value):\n    return value.lower()\n",
            "tests/test_names.py": "import unittest\nfrom names import normalize\n\nclass Tests(unittest.TestCase):\n    def test_spaces(self):\n        self.assertEqual(normalize('  Acme  '), 'acme')\n    def test_unicode(self):\n        self.assertEqual(normalize('Straße'), normalize('STRASSE'))\n    def test_empty(self):\n        self.assertEqual(normalize('   '), '')\n    def test_no_mutation(self):\n        original=' X '; normalize(original); self.assertEqual(original,' X ')\n",
        }, "verify_command": ["python", "-m", "unittest", "discover", "-s", "tests"],
    },
    "heldout_file_organization": {
        "goal": "请把 inbox 中的收据按内容里的开票月份整理到 整理/YYYY-MM/ 下，保留原文件名。只创建副本，原件和备注都不要修改或删除。文件名可能有中文和空格，不要用文件修改时间或文件名推断月份；完成后说明处理了哪些文件，并验证副本和原件一致。",
        "files": {
            "README.md": "Receipts are CSV files under inbox/. The issued_at column contains the authoritative invoice date. Notes are not receipts.\n",
            "inbox/收据 a.csv": "issued_at,reference,amount,note\n2026-09-30,RCP-731,129.90,\"中文内容,保留逗号\"\n",
            "inbox/October misleading.csv": "issued_at,reference,amount,note\n2026-09-28,RCP-812,70.00,September invoice despite filename\n",
            "inbox/收据 b.csv": "issued_at,reference,amount,note\n2026-10-01,RCP-953,44.20,keep original bytes\n",
            "inbox/备注.txt": "这份备注不是收据，不能修改。\n",
        },
        "expected_copies": {
            "整理/2026-09/收据 a.csv": "inbox/收据 a.csv",
            "整理/2026-09/October misleading.csv": "inbox/October misleading.csv",
            "整理/2026-10/收据 b.csv": "inbox/收据 b.csv",
        },
        "facts": ["2026-09", "2026-10"],
    },
    "heldout_mail_routine_actions": {
        "goal": "请邮件专家阅读全部本地邮件，整理我确实需要采取的行动，包括不紧急的例行事项；不要把纯通知当待办。不做远程同步，说明实际覆盖范围。",
        "files": {}, "read_only": True, "children_required": 1, "required_agent_id": "mail_expert",
        "setup": {"mail": {"account": {"email_address": "routine@example.test"}, "messages": [
            {"external_id": "optional", "subject": "Monthly information", "sender": "updates@example.test",
             "received_at": "2026-10-04T09:00:00Z", "body_text": "本月资讯已发布，仅供参考，无需回复或采取行动。"},
            {"external_id": "routine", "subject": "Profile maintenance", "sender": "office@example.test",
             "received_at": "2026-10-04T10:00:00Z",
             "body_text": "例行维护：请在 2026-11-15 前更新 contact-card.csv，归档编号 CARD-846。距离截止还有一个多月，不紧急，但需要本人完成。"},
            {"external_id": "receipt", "subject": "Acknowledgment", "sender": "office@example.test",
             "received_at": "2026-10-04T11:00:00Z", "body_text": "你提交的旧周报已确认收到，此事已完成，没有后续任务。"},
        ]}}, "facts": ["contact-card.csv", "CARD-846", "2026-11-15"],
    },
    "heldout_file_collision": {
        "goal": "请把 inbox 下各子目录的收据按 CSV 内容中的 issued_at 月份创建副本到 整理/YYYY-MM/，副本以 reference.csv 命名。同名原件可能不是同一张收据，不要覆盖或遗漏；日期缺失的先保留原位并说明不能归类。不要修改或删除任何原件、备注。验证有效收据副本逐字节一致。",
        "files": {
            "README.md": "Nested CSV receipts use issued_at and reference as authoritative fields. Never infer dates from folders or filenames.\n",
            "inbox/来源 A/收据.csv": "issued_at,reference,amount\r\n2026-08-31,RCP-274,73.00\r\n",
            "inbox/来源 B/收据.csv": "issued_at,reference,amount\n2026-08-29,RCP-936,129.00\n",
            "inbox/2026-08 misleading/收据.csv": "issued_at,reference,amount\n2026-09-01,RCP-581,10.50\n",
            "inbox/未知日期/收据.csv": "issued_at,reference,amount\n,RCP-611,33.00\n",
            "inbox/备注.txt": "The undated receipt must stay unclassified. Original bytes are authoritative.\n",
        },
        "expected_copies": {
            "整理/2026-08/RCP-274.csv": "inbox/来源 A/收据.csv",
            "整理/2026-08/RCP-936.csv": "inbox/来源 B/收据.csv",
            "整理/2026-09/RCP-581.csv": "inbox/2026-08 misleading/收据.csv",
        },
        "facts": ["2026-08", "2026-09", "RCP-611"],
    },
    "heldout_web_multisource": {
        "goal": "请查 Python 官方资料，对照 3.13 和 3.14 的 free-threaded 支持状况：分别是不是默认开启、支持级别有何变化、第三方扩展为什么可能让 GIL 重新启用。至少核对两个相关官方页面，附来源。不要把默认构建与可选构建混为一谈，结论简短。",
        "files": {}, "web": True, "search_required": False, "read_only": True,
        "minimum_page_sources": 2, "source_hosts": ["docs.python.org"],
        "facts": ["3.13", "3.14", "GIL"],
    },
    "heldout_context_reuse": {
        "goal": "请查看这个项目资料，告诉我当前状态和唯一标识。",
        "files": {"README.md": "Project status: jade. Unique marker: willow-739.\n"},
        "facts": ["jade", "willow-739"], "read_only": True,
        "followup_goal": "只再告诉我刚才那个唯一标识，简短即可。",
        "followup_facts": ["willow-739"],
    },
    "heldout_web_sqlite": {
        "goal": "请从 SQLite 官方资料核实 WAL 模式的并发限制：是否允许多个写者同时写，读事务看到哪个时点的数据，以及长读事务会怎样影响 checkpoint。核对至少两个相关官方页面，别只根据搜索摘要回答，结论简短且附来源。",
        "files": {}, "web": True, "search_required": False, "read_only": True,
        "minimum_page_sources": 2, "source_hosts": ["sqlite.org", "www.sqlite.org"],
        "facts": ["WAL", "checkpoint"],
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


def _copy_checks(before: dict[str, str], after: dict[str, str],
                 expected: dict[str, str]) -> dict[str, bool]:
    """Check actual bytes and original preservation independently of the answer."""
    return {
        "originals_preserved": all(after.get(path) == digest for path, digest in before.items()),
        "copies_match_original_bytes": all(source in before and after.get(destination) == before[source]
                                           for destination, source in expected.items()),
        "no_unexpected_files": set(after) == set(before) | set(expected),
    }


def _page_evidence_urls(tool_events: list, *, source_hosts: list[str] | None = None) -> set[str]:
    """Count delivered page evidence, not search candidates or semantic verification.

    Re-reading offsets, fragments or query variants of one static page does not
    satisfy a multiple-page goal. No-match find results are not page evidence.
    """
    pages = set()
    hosts = {host.casefold() for host in source_hosts} if source_hosts is not None else None
    for event in tool_events:
        if event.tool_name not in {"web.open", "web.find"} or event.result.get("status") != "completed":
            continue
        output = event.result.get("output", {})
        if not isinstance(output, dict):
            continue
        text = output.get("text")
        matches = output.get("matches", [])
        delivered = (
            isinstance(text, str) and bool(text.strip()) if event.tool_name == "web.open"
            else isinstance(matches, list) and any(
                isinstance(match, dict) and isinstance(match.get("snippet"), str)
                and bool(match["snippet"].strip()) for match in matches
            )
        )
        url = output.get("url")
        if not delivered or not isinstance(url, str):
            continue
        try:
            parsed = urlsplit(url)
            host = parsed.hostname
            if (parsed.scheme != "https" or not host or parsed.username is not None
                    or parsed.password is not None or parsed.port not in {None, 443}
                    or (hosts is not None and host not in hosts)):
                continue
        except ValueError:
            continue
        pages.add(urlunsplit(("https", host, parsed.path.rstrip("/") or "/", "", "")))
    return pages


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


async def _followup_probe(runtime, *, session_id: str, case: dict, timeout: float) -> dict:
    """Reuse the actual persisted session, not a manually fabricated summary."""
    goal = case["followup_goal"]
    run = runtime.create_agent_run(session_id=session_id, user_input=goal)
    started = time.perf_counter()
    report = {"run_id": run.run_id, "goal": goal}
    try:
        result = await asyncio.wait_for(runtime.run_agent_turn_async(
            session_id=session_id, user_input=goal, existing_run_id=run.run_id), timeout=timeout)
        report["result"] = result.model_dump(mode="json")
        report["checks"] = {
            "fact_coverage": all(fact.casefold() in result.answer.casefold()
                                 for fact in case["followup_facts"]),
            "no_redundant_tools": not result.tool_events,
        }
        report["metrics"] = {
            "llm_calls": len(result.llm_events), "tool_calls": len(result.tool_events),
            "input_tokens": sum(event.input_token_count or 0 for event in result.llm_events),
            "output_tokens": sum(event.output_token_count or 0 for event in result.llm_events),
        }
        report["mechanical_pass"] = all(report["checks"].values())
    except Exception as exc:  # noqa: BLE001 - preserve follow-up failure independently
        report["error"] = {"type": type(exc).__name__, "message": str(exc)}
        report["mechanical_pass"] = False
        if isinstance(exc, TimeoutError):
            runtime.agent_run_manager.cancel_run(run.run_id, reason="isolated follow-up timeout")
    finally:
        report["task_wall_seconds"] = round(time.perf_counter() - started, 3)
        report["run_events"] = [event.model_dump(mode="json")
                                for event in runtime.agent_run_manager.list_events(run.run_id)]
    return report


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
                   mail_expert: bool = False, full_retrieval: bool = False,
                   response_mode: str = "text") -> dict:
    if protocol not in {"configured", "json", "native"}:
        raise ValueError("unsupported protocol experiment")
    if response_mode not in {"text", "stream"}:
        raise ValueError("unsupported response mode experiment")
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
              "planning": planning, "protocol": protocol, "response_mode": response_mode,
              "session_id": session_id, "private_artifacts": str(root),
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
        mode_options = {"llm_response_mode": LLMResponseMode.STREAM} if response_mode == "stream" else {}
        result = await asyncio.wait_for(runtime.run_agent_turn_async(
            session_id=session_id, user_input=case["goal"], existing_run_id=evaluation_run.run_id,
            **mode_options), timeout=timeout)
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
        if case.get("expected_copies"):
            checks.update(_copy_checks(before, _hashes(workspace), case["expected_copies"]))
        if case.get("verify_command"):
            completed = await asyncio.to_thread(subprocess.run, case["verify_command"],
                cwd=workspace, capture_output=True, text=True, timeout=15, check=False)
            report["independent_verification"] = {"exit_code": completed.returncode,
                "stdout": completed.stdout[-8000:], "stderr": completed.stderr[-8000:]}
            checks["independent_tests_pass"] = completed.returncode == 0
            checks["tests_not_modified"] = all(_hashes(workspace).get(p) == h
                for p, h in before.items() if p.startswith("tests/"))
        if case.get("web"):
            pages = _page_evidence_urls(result.tool_events, source_hosts=case.get("source_hosts"))
            checks["page_evidence_loaded"] = bool(pages)
            if case.get("minimum_page_sources"):
                checks["required_page_sources_loaded"] = len(pages) >= case["minimum_page_sources"]
        if case.get("search_required"):
            checks["search_executed"] = any(e.tool_name == "web.search" and e.result.get("status") == "completed"
                                             for e in result.tool_events)
        # Coverage and a successful fetch are necessary, not semantic-proof scores.
        report["checks"] = checks
        report["mechanical_pass"] = all(checks.values())
        first_progress = next((event for event in result.progress_events
                               if event.type == "assistant_message"), None)
        answer_delta = next((event for event in runtime.agent_run_manager.list_events(result.run_id)
                             if event.type == "llm_delta" and event.stage == "answer"
                             and event.payload.get("content_role") == "final_answer"
                             and str(event.payload.get("delta", "")).strip()), None)
        report["metrics"] = {"first_agent_progress_seconds":
            round((datetime.fromisoformat(first_progress.created_at) - started_at).total_seconds(), 3)
            if first_progress else None,
            "first_final_token_seconds":
                round((datetime.fromisoformat(answer_delta.created_at) - started_at).total_seconds(), 3)
                if answer_delta else None,
            "response_mode": response_mode,
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
        if case.get("followup_goal"):
            report["initial_turn_seconds"] = round(time.perf_counter() - started, 3)
            report["followup"] = await _followup_probe(runtime, session_id=session_id, case=case, timeout=timeout)
            checks["followup_fact_and_reuse"] = report["followup"]["mechanical_pass"]
            if case.get("read_only"):
                checks["files_unchanged"] = _hashes(workspace) == before
            report["mechanical_pass"] = all(checks.values())
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
    parser.add_argument("--response-mode", choices=["text", "stream"], default="text",
                        help="measure first final-answer delta from persisted backend events; not frontend/network TTFT")
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
                                     full_retrieval=args.full_retrieval, response_mode=args.response_mode))
        print(json.dumps({k: report.get(k) for k in
              ("case_id", "task_wall_seconds", "mechanical_pass", "checks", "error", "private_artifacts",
               "budget_after")}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
