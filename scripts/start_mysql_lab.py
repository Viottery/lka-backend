"""Start an isolated MySQL-backed API with mock models and private lab credentials."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--connection-file", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--workbench-dir", type=Path)
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--hub", action="store_true",
                        help="Enable isolated account/device/sync hub APIs (loopback HTTP only)")
    parser.add_argument("--reply-delay-ms", type=int, default=0,
                        help="Synthetic model delay per call, outside DB transactions (0..2000 ms)")
    args = parser.parse_args()
    if not 0 <= args.reply_delay_ms <= 2000:
        parser.error("--reply-delay-ms must be between 0 and 2000")
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    credentials = json.loads(args.connection_file.expanduser().resolve(strict=True).read_text())
    allowed = {"host", "port", "user", "password", "database"}
    if set(credentials) - allowed or not {"host", "user", "password", "database"} <= credentials.keys():
        raise SystemExit("Expected private lab host/port/user/password/database JSON")
    if not any(part in credentials["database"].lower() for part in ("test", "lab")):
        raise SystemExit("This launcher requires an explicitly named test/lab database")
    data = args.data_dir.expanduser().resolve()
    if (data / "lka.sqlite3").exists() or data == (root / "data/runtime").resolve():
        raise SystemExit("Refusing an existing SQLite authority or everyday data directory")
    data.mkdir(parents=True, exist_ok=True)
    data.chmod(0o700)
    workspace = data / "workspace"
    workspace.mkdir(exist_ok=True)
    config = data / "mysql-lab.toml"
    content = '''[llm]
provider="mock"
[agent]
unified_entry_enabled=true
orchestrator="langgraph"
checkpoint_backend="sqlite"
[management]
enabled=true
autonomous_enabled=false
[memory]
enabled=false
background_enabled=false
[message_history]
enabled=false
background_enabled=false
[embedding]
enabled=false
[reranker]
enabled=false
'''
    content = content.replace("autonomous_enabled=false", "autonomous_enabled=false\ndefault_task_parent=" + json.dumps(str(workspace)))
    if args.hub:
        content += f'''
[auth]
enabled=true
require_https=false
secure_cookie=false
allowed_origins=["http://127.0.0.1:{args.port}","http://localhost:{args.port}"]
[devices]
enabled=true
[sync]
enabled=true
role="hub"
[connections]
enabled=true
[remote_execution]
enabled=true
'''
    if args.workbench_dir:
        content += '\n[client]\nworkbench_dir=' + json.dumps(str(args.workbench_dir.resolve(strict=True))) + '\n'
    config.write_text(content, encoding="utf-8")
    for name in list(os.environ):
        if name.startswith(("LKA_", "OPENAI_", "ANTHROPIC_", "MS_GRAPH_", "MAIL_", "IMAP_", "SMTP_")):
            os.environ.pop(name, None)
    os.environ.update(LKA_DATA_DIR=str(data), LKA_LOCAL_CONFIG=str(config),
                      LKA_STORAGE_BACKEND="mysql", LKA_WORKSPACE_ROOTS=str(workspace))
    for name, value in credentials.items():
        os.environ["LKA_MYSQL_" + name.upper()] = str(value)
    from app.core.config import Settings, get_settings

    Settings.model_config["env_file"] = None
    get_settings.cache_clear()
    settings = Settings()
    assert settings.data_dir.resolve() == data and settings.storage_backend == "mysql"
    print(f"Isolated MySQL API: http://127.0.0.1:{args.port}; models: mock", flush=True)
    import uvicorn

    from app.api.main import app
    from app.core.llm import LLMResponse

    class LabReplyClient:
        """Protocol-valid synthetic replies; never selects tools or a provider."""

        def complete_text(self, **kwargs):
            if args.reply_delay_ms:
                time.sleep(args.reply_delay_ms / 1000)
            stage = kwargs.get("metadata", {}).get("stage")
            if stage == "answer_review":
                # This is a protocol fixture, not a semantic quality verdict.
                content = json.dumps({"faithful": True, "hallucination_free": True,
                                      "request_aligned": True, "decision": "pass", "issues": [],
                                      "revision_instructions": ""})
            else:
                content = (json.dumps({"operation": {"type": "final_answer", "reason": "Isolated storage acceptance"}})
                           if stage in {"decision", "decision_repair"}
                           else "MySQL 隔离测试回复：会话与运行事件已进入测试存储；未调用外部模型。")
            return LLMResponse(provider="mysql_lab_mock", status="completed", content=content,
                               prompt_summary=kwargs["prompt_summary"])

    # The normal provider=mock configuration deliberately has no Agent Brain.
    # Install a script-local fixture so browser acceptance has a valid final
    # answer instead of recording answer_generation_failed as a false success.
    app.state.runtime.agent_turn_loop.llm_client = LabReplyClient()
    uvicorn.run(app, host="127.0.0.1", port=args.port, workers=1)


if __name__ == "__main__":
    main()
