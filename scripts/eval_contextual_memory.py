"""Small live semantic probe; isolated SQLite, synthetic conversations only."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
from pathlib import Path

from app.core.background_llm import SelectedBackgroundClient
from app.core.llm import build_text_llm_client
from app.core.local_config import _dotenv_value, load_local_config
from app.core.memory_files import MemoryFiles
from app.core.memory_learning import ContextualMemoryLearning
from app.core.sessions import SessionService
from app.domains.memory import MemoryService
from app.storage.db import connect, init_db


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    config = load_local_config(args.config)
    for client in config.llm.client_configs():
        if not os.getenv(client.api_key_env):
            value = _dotenv_value(client.api_key_env, args.config.parent.parent / ".env")
            if value:
                os.environ[client.api_key_env] = value
    service = build_text_llm_client(config.llm)
    if service is None:
        raise SystemExit("No configured model service")
    selected = SelectedBackgroundClient(
        service, config.memory.background_client_name, config.memory.background_model,
        initial_output_tokens=2048, recovery_output_tokens=4096,
    )
    cases = [
        ("roleplay_confirmation", [
            ("user", "搜索一下明日方舟角色真理。我希望后续一直模仿真理的性格和口吻，不改变工作能力，只改变说话方式。"),
            ("agent", "可以，只改变交流风格，仍然保持原有工作能力。"),
            ("user", "我希望你一直这样哦，把这个要求保存到全局记忆吧"),
        ]),
        ("durable_user_fact", [("user", "我平时住在上海，推荐周末活动时考虑这个距离。")]),
        ("one_time_task", [("user", "帮我搜索一下明天上海的天气，不用记住这件事。")]),
        ("scoped_negative_preference", [("user", "分析代码的时候我不想看大段背景，但讨论研究问题时请讲清楚推导，不要把所有回答都变短。")]),
    ]
    reports = []
    with tempfile.TemporaryDirectory(prefix="lka-memory-probe-") as temporary:
        root = Path(temporary)
        db = root / "probe.sqlite3"
        sessions = SessionService(lambda: connect(db))
        init_db(db)
        memory = MemoryService(db)
        memory.ensure_schema()
        learning = ContextualMemoryLearning(memory, sessions, MemoryFiles(memory, root))
        for case_id, conversation in cases:
            for role, text in conversation:
                message = sessions.append_message(session_id=case_id, role=role, content=text)
            start = time.monotonic()
            records, file_status = learning.organize(message, client=selected)
            reports.append({"case": case_id, "elapsed_seconds": round(time.monotonic() - start, 3),
                            "memories": [{"content": r.content, "status": r.status,
                                          "scope": r.scope, "expires_at": r.expires_at} for r in records],
                            "file_status": file_status})
    print(json.dumps({"cases": reports}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
