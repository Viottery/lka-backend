"""Scoped native-Windows deployment/activation of the QQ reading pilot.

Run with the native backend interpreter, not WSL SQLite. Secrets stay in the
native environment; output is counters/configuration only. Historical cache is
archived before revoking old capture policies. No QQ API is called.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import shutil
import socket
import sqlite3
import sys
import tomllib
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

BACK = Path("C:/Users/xc133/projects/lka_backend")
FRONT = Path("D:/agent-bot-frontend")
SOURCE = Path(__file__).resolve().parents[1]
DB = BACK / "data/runtime/lka.sqlite3"
ACCOUNT = "1330948336"
GROUPS = ("1036840759", "1046158144", "827451099")
FILES = (
    "app/api/routes/message_reading.py",
    "app/domains/message_history.py",
    "app/domains/message_reading_results.py",
    "app/domains/message_reading_runtime.py",
    "app/domains/message_history_tracking.py",
    "app/domains/message_participant_profiles.py",
    "app/domains/message_profile_documents.py",
    "app/domains/message_reading_codec.py",
    "app/domains/message_reading_intelligence.py",
    "app/domains/message_reading_metrics.py",
    "app/domains/message_reading_replay.py",
    "app/core/message_analysis.py",
    "app/tool_packages/messages.py",
    "app/tool_packages/message_history_analysis.py",
    "app/tool_packages/message_reading_analysis.py",
    "app/tool_packages/message_replay_analysis.py",
)
SETTINGS = {
    "enabled": True, "background_enabled": True, "reading_algorithm": "selected",
    "worker_count": 1, "selector_max_messages": 40,
    "max_input_tokens": 16000, "generation_output_tokens": 6500,
    "recovery_output_tokens": 6500, "max_job_tokens": 90000, "max_job_calls": 4,
    "service_hourly_token_limit": 50000, "service_daily_token_limit": 200000,
    "service_hourly_call_limit": 8, "service_daily_call_limit": 48,
    "conversation_hourly_token_limit": 35000, "conversation_daily_token_limit": 80000,
    "conversation_hourly_call_limit": 4, "conversation_daily_call_limit": 24,
}
HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def emit(event, **fields):
    print(json.dumps({"event": event, **fields}, ensure_ascii=False), flush=True)


def request(path, payload=None, method=None, frontend=False):
    from dotenv import dotenv_values
    token = dotenv_values(BACK / ".env").get("LKA_MESSAGES_API_TOKEN")
    headers = {}
    if token and not frontend:
        headers["Authorization"] = "Bearer " + token
    body = None if payload is None else json.dumps(payload).encode()
    if body is not None:
        headers["Content-Type"] = "application/json"
    base = "http://127.0.0.1:8780" if frontend else "http://127.0.0.1:8765"
    with HTTP.open(urllib.request.Request(base + path, data=body, headers=headers, method=method), timeout=20) as response:
        return json.load(response)


def inspect():
    config = tomllib.loads((BACK / "config/local.toml").read_text(encoding="utf-8"))
    llm = config.get("llm", {})
    emit("model_configuration", default_client=llm.get("default_client"), model=llm.get("model"),
         clients=[{k: v for k, v in row.items() if k in ("name", "provider", "default_model", "available_models", "context_window_tokens")}
                  for row in llm.get("clients", [])], message_history=config.get("message_history", {}))
    reader = request("/plugins/qq-reader/status", frontend=True)
    emit("health", backend=request("/health"), frontend=request("/health", frontend=True), reader=reader)
    with sqlite3.connect(DB.as_uri() + "?mode=ro", uri=True) as conn:
        runs = conn.execute("SELECT COUNT(*) FROM agent_runs WHERE status IN ('queued','running','waiting_confirmation','waiting_user')").fetchone()[0]
        jobs = conn.execute("SELECT COUNT(*) FROM background_jobs WHERE status='running'").fetchone()[0]
        emit("preflight", active_runs=runs, active_jobs=jobs)
        if runs or jobs or reader.get("pending_count") or reader.get("media", {}).get("counters", {}).get("pending"):
            raise RuntimeError("active_work_prevents_restart")


def stopped():
    for port in (8765, 8780):
        with socket.socket() as connection:
            connection.settimeout(1)
            if connection.connect_ex(("127.0.0.1", port)) == 0:
                raise RuntimeError("stop_native_services_before_deployment")


def replace_class(original, replacement, name):
    def locate(text):
        tree = ast.parse(text)
        node = next(item for item in tree.body if isinstance(item, ast.ClassDef) and item.name == name)
        lines = text.splitlines(keepends=True)
        return lines, node.lineno - 1, node.end_lineno
    lines, start, end = locate(original)
    fresh, a, b = locate(replacement)
    return "".join(lines[:start] + fresh[a:b] + lines[end:])


def deploy():
    stopped()
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup = BACK / "data/deployment_backups" / ("message-reading-" + stamp)
    backup.mkdir(parents=True, exist_ok=False)
    # SQLite backup reads the complete native WAL snapshot, not just the main file.
    from dotenv import dotenv_values
    front_env = dotenv_values(FRONT / ".env")
    reader_db = FRONT / front_env.get("QQ_DB_PATH", "data/qq/reader.db")
    for source, name in ((DB, "backend.sqlite3"), (reader_db, "reader.sqlite3")):
        with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as old, sqlite3.connect(backup / name) as new:
            old.backup(new)
    media = FRONT / front_env.get("QQ_MEDIA_CACHE_DIR", "data/qq/media")
    if media.exists():
        shutil.copytree(media, backup / "qq-media")
    config_path = BACK / "config/local.toml"
    shutil.copy2(config_path, backup / "local.toml")
    manifest = []
    for relative in (*FILES, "app/core/local_config.py", "app/core/runtime.py"):
        target = BACK / relative
        if target.exists():
            saved = backup / "source" / relative
            saved.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, saved)
        target.parent.mkdir(parents=True, exist_ok=True)
        if relative in FILES:
            content = (SOURCE / relative).read_text(encoding="utf-8")
        elif relative.endswith("local_config.py"):
            content = replace_class(target.read_text(encoding="utf-8"), (SOURCE / relative).read_text(encoding="utf-8"), "MessageHistoryConfig")
        else:
            # Only transplant the message processing binding, not unrelated runtime changes.
            content = target.read_text(encoding="utf-8")
            marker = "from app.tool_packages.messages import ("
            new_import = "from app.tool_packages.message_reading_analysis import VERSIONS as MESSAGE_READING_VERSIONS\n"
            if new_import not in content:
                content = content.replace(marker, new_import + marker, 1)
            def binding(text):
                node = next(n for n in ast.walk(ast.parse(text)) if isinstance(n, ast.Call)
                            and isinstance(n.func, ast.Name) and n.func.id == "bind_reading_configuration")
                lines = text.splitlines(keepends=True)
                return lines, node.lineno - 1, node.end_lineno
            lines, a, b = binding(content)
            fresh, c, d = binding((SOURCE / relative).read_text(encoding="utf-8"))
            content = "".join(lines[:a] + fresh[c:d] + lines[b:])
        compile(content, relative, "exec")
        target.write_text(content, encoding="utf-8", newline="\n")
        manifest.append({"file": relative, "sha256": hashlib.sha256(content.encode()).hexdigest()})
    config = config_path.read_text(encoding="utf-8")
    parsed = tomllib.loads(config)
    if "message_history" in parsed:
        raise RuntimeError("existing_message_settings_require_merge_not_overwrite")
    extra = "\n[message_history]\n" + "".join(f"{key} = {json.dumps(value)}\n" for key, value in SETTINGS.items())
    config_path.write_text(config.rstrip() + "\n" + extra, encoding="utf-8", newline="\n")
    sys.path.insert(0, str(BACK))
    from app.core.background_jobs import BackgroundJobStore
    from app.core.local_config import load_local_config
    from app.domains.message_history import MessageHistoryService
    from app.domains.message_reading_results import ReadingProfile
    loaded = load_local_config(config_path)  # Validate before startup.
    jobs = BackgroundJobStore(DB)
    jobs.ensure_schema()
    service = MessageHistoryService(DB, jobs)
    service.ensure_schema()
    service.configure_reading(loaded.message_history.model_dump(mode="json"))
    control = service.reading_status()
    if not control["paused"]:
        service.set_reading_paused(True, control["revision"])
    # Correct @me/reply-to-self detection without guessing names or user interests.
    profile = service.get_reading_profile()
    value = {key: profile[key] for key in ReadingProfile.model_fields}
    identities = value["self_ids"].setdefault("qq", [])
    if ACCOUNT not in identities:
        identities.append(ACCOUNT)
        service.set_reading_profile(value, profile["revision"])
    # Reconcile while stopped: the collector must never reload an old whitelist
    # during the start/activate gap. Keep model work paused until final verification.
    reconcile_policies(service.list_policies, service.set_policy)
    # Retain the two previously authorized living dossiers; never activate the old third group.
    history = SOURCE / "data/message_replay/living_profiles"
    destination = DB.parent / "message_profiles"
    for group_dir in ("59a9508dbe95d1580b3eadc3", "f988731e97a3495e26f05108"):
        origin = history / group_dir
        target = destination / group_dir
        if origin.is_dir() and not target.exists():
            shutil.copytree(origin, target)
    (backup / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    emit("deployed", files=len(manifest), backup=str(backup), profile_root=str(destination))


def reconcile_policies(get_policies, update_policy):
    policies = get_policies()
    for row in policies:
        if row["platform"] != "qq" or row["account_id"] != ACCOUNT:
            continue
        keep = row["conversation_type"] == "group" and row["conversation_id"] in GROUPS
        if keep:
            continue
        if any(row.get(field) for field in ("record_enabled", "analysis_enabled", "media_enabled")):
            payload = {k: row[k] for k in ("platform", "account_id", "conversation_type", "conversation_id", "display_name", "batch_size", "timezone")}
            payload.update(expected_revision=row["revision"], record_enabled=False, analysis_enabled=False, media_enabled=False, proposals_enabled=False)
            update_policy(payload)
            emit("capture_disabled", conversation_type=row["conversation_type"], conversation_id=row["conversation_id"], historical_messages_preserved=True)
    for group in GROUPS:
        policies = get_policies()
        row = next((p for p in policies if p["platform"] == "qq" and p["account_id"] == ACCOUNT and p["conversation_type"] == "group" and p["conversation_id"] == group), None)
        payload = {"platform": "qq", "account_id": ACCOUNT, "conversation_type": "group", "conversation_id": group,
                   "display_name": row["display_name"] if row else group, "expected_revision": row["revision"] if row else 0,
                   "record_enabled": True, "analysis_enabled": True, "media_enabled": True, "proposals_enabled": False,
                   "local_signals_enabled": True, "minimum_import_version": 2, "timezone": "Asia/Shanghai",
                   "batch_size": 100, "min_interval_seconds": 1800, "max_wait_seconds": 7200, "max_batch_messages": 140,
                   "auto_analyze": True}
        if row and all(row.get(key) == value for key, value in payload.items() if key != "expected_revision"):
            continue
        if not row or not row["analysis_enabled"]:
            payload["start_from_now"] = True
        result = update_policy(payload)
        emit("capture_and_analysis_enabled", conversation_id=group, revision=result["revision"])


def activate():
    reconcile_policies(lambda: request("/messages/policies")["policies"],
                       lambda payload: request("/messages/policies", payload, "PUT")["policy"])
    policies = request("/messages/policies")["policies"]
    active = [p for p in policies if p["platform"] == "qq" and p["account_id"] == ACCOUNT and p["record_enabled"]]
    assert {p["conversation_id"] for p in active} == set(GROUPS)
    assert all(p["conversation_type"] == "group" and p["analysis_enabled"] for p in active)
    status = request("/background/services/message-reading")
    if status["paused"]:
        request("/background/services/message-reading/resume", {"expected_revision": status["revision"]}, "POST")
    emit("reading_resumed", groups=list(GROUPS))


def verify():
    from dotenv import dotenv_values

    policies = request("/messages/policies")["policies"]
    actual = {p["conversation_id"] for p in policies if p["platform"] == "qq" and p["account_id"] == ACCOUNT and p["record_enabled"]}
    assert actual == set(GROUPS), "incorrect_active_capture_scope"
    for p in policies:
        if p["platform"] != "qq" or p["account_id"] != ACCOUNT:
            continue
        with sqlite3.connect(DB.as_uri() + "?mode=ro", uri=True) as conn:
            count, last = conn.execute("SELECT COUNT(*),MAX(received_at) FROM message_history_messages WHERE conversation_key=?", (p["conversation_key"],)).fetchone()
            state = conn.execute("SELECT * FROM message_history_conversations WHERE conversation_key=?", (p["conversation_key"],)).fetchone()
            columns = [r[1] for r in conn.execute("PRAGMA table_info(message_history_conversations)")]
        values = dict(zip(columns, state))
        emit("policy_verified", conversation_id=p["conversation_id"], conversation_type=p["conversation_type"],
             record_enabled=p["record_enabled"], analysis_enabled=p["analysis_enabled"], media_enabled=p["media_enabled"],
             messages=count, last_received_at=last, analysis_floor=values.get("analysis_baseline_floor_seq"),
             covered_seq=values.get("covered_seq"), generation_published_seq=values.get("generation_published_seq"))
        if p["record_enabled"]:
            folder = hashlib.sha256(p["conversation_key"].encode()).hexdigest()[:24]
            state_path = DB.parent / "message_profiles" / folder / "state.json"
            if state_path.exists():
                people = json.loads(state_path.read_text(encoding="utf-8"))["participants"]
                emit("local_dossiers", conversation_id=p["conversation_id"], people=len(people),
                     observations=sum(len(person.get("claims", [])) for person in people.values()),
                     unverified_notes=sum(len(person.get("machine_notes", [])) for person in people.values()))
    emit("reading_service", **request("/background/services/message-reading"))
    emit("reader", **request("/plugins/qq-reader/status", frontend=True))
    reader_path = FRONT / dotenv_values(FRONT / ".env").get("QQ_DB_PATH", "data/qq/reader.db")
    with sqlite3.connect(reader_path.as_uri() + "?mode=ro", uri=True) as conn:
        approved = conn.execute("SELECT conversation_type,conversation_id FROM approved_policies WHERE platform='qq' AND account_id=?", (ACCOUNT,)).fetchall()
    assert set(approved) == {("group", group) for group in GROUPS}, "collector_has_stale_whitelist"
    emit("collector_whitelist_verified", groups=sorted(group for _, group in approved))


def smoke():
    """Queue at most one new-message tail through normal budgeted production work."""
    policies = request("/messages/policies")["policies"]
    for group in GROUPS:
        policy = next(p for p in policies if p["platform"] == "qq" and p["account_id"] == ACCOUNT
                      and p["conversation_type"] == "group" and p["conversation_id"] == group)
        key = policy["conversation_key"]
        coverage = request(f"/messages/conversations/{key}/coverage")["coverage"]
        if coverage["pending_messages"] < 3:
            continue
        result = request(f"/messages/conversations/{key}/analyze", {}, "POST")
        emit("bounded_live_smoke", conversation_id=group, pending=coverage["pending_messages"],
             status=result["status"], jobs=[{k: j.get(k) for k in ("job_id", "status", "error_class")} for j in result["jobs"]])
        return
    emit("bounded_live_smoke", status="waiting_for_three_new_messages")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("inspect", "deploy", "activate", "verify", "smoke"))
    args = parser.parse_args()
    if sys.platform != "win32":
        raise SystemExit("Use the native Windows interpreter")
    globals()[args.action]()
