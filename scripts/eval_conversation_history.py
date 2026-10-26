"""Synthetic Chinese history retrieval with an already cached local model.

No downloads, remote inference, production data, or business tools are used.
Timings include source revalidation and SQL. This small smoke set is not a
general retrieval-quality benchmark.
"""

from __future__ import annotations

import argparse
import json
import statistics
import tempfile
from pathlib import Path
from time import perf_counter

from app.core.local_config import EmbeddingConfig
from app.core.sessions import SessionService
from app.core.tools import ToolContext
from app.domains.conversation_history import ConversationHistoryService
from app.domains.conversation_semantic import ConversationSemanticIndex
from app.integrations.local_semantic import FastEmbedEmbeddingProvider
from app.storage.db import connect, init_db

CASES = [
    ("我想给朋友挑一款生日游戏礼物，希望轻度玩家也能喜欢。", "同学生日送什么电子游戏合适？"),
    ("项目文件请保存在D盘，避免占用系统盘。", "工作目录应该选哪个磁盘？"),
    ("邮件早报每天上午八点发送。", "收件箱晨间简报的推送时间是什么？"),
    ("不需要电脑工作区的任务，助理应当在云服务器执行。", "没有本地文件需求的工作交给哪台机器？"),
    ("有风险的工具调用被模型拒绝后，让智能体调整方案继续处理。", "危险操作拦截之后应该怎么做？"),
    ("会话摘要只负责维护当前对话方向，具体细节由原始记录保存。", "压缩后的中期记忆应该记住什么？"),
    ("任务树应该展示每个子智能体的耗时、消耗和完整对话。", "在哪里看分支代理运行了多久？"),
    ("云端下线时，本地电脑仍然可以进行普通聊天。", "服务器断网还能继续对话吗？"),
    ("学习到的记忆自动同步到其他设备，并沿用原有生效状态。", "自动发现的偏好怎样在多台电脑间共享？"),
    ("桌宠菜单提供仅退出形象窗口、保留后台服务的选项。", "关闭桌面小动物时能不结束后端吗？"),
    ("使用账户名和密码登录，设备另行登记配对。", "身份验证是怎样进入系统的？"),
    ("数据库写操作进入单写者队列，事务有超时和取消机制。", "很多并发写入如何防止锁竞争？"),
]


def main() -> None:
    defaults = EmbeddingConfig()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", type=Path, default=defaults.cache_dir)
    parser.add_argument("--model-name", default=defaults.model_name)
    parser.add_argument("--dimensions", type=int, default=defaults.dimensions)
    parser.add_argument("--rounds", type=int, default=20)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 1 <= args.rounds <= 100:
        parser.error("--rounds must be in 1..100")
    provider = FastEmbedEmbeddingProvider(
        model_name=args.model_name, dimensions=args.dimensions,
        cache_dir=str(args.cache_dir), batch_size=16,
        query_prefix=defaults.query_prefix, local_files_only=True, threads=2,
    )
    with tempfile.TemporaryDirectory(prefix="lka-history-local-eval-") as temp:
        db = Path(temp) / "isolated.sqlite3"
        init_db(db)
        factory = lambda: connect(db)
        sessions = SessionService(factory)
        sid = sessions.create_session(title="Synthetic history retrieval").session.session_id
        for i, (statement, _) in enumerate(CASES):
            sessions.record_context_exchange(session_id=sid, user_input=statement,
                agent_answer="已记录这项要求。", trace_id=f"synthetic-{i}")
        index = ConversationSemanticIndex(factory, provider)
        started = perf_counter()
        status = index.sync_batch(sid)
        if status["state"] == "unavailable":
            raise SystemExit("Local embedding unavailable; install/cache the configured model explicitly first.")
        while status["pending"]:
            status = index.sync_batch(sid)
            if status["state"] == "unavailable":
                raise SystemExit("Local indexing failed; pending work remains retryable.")
        index_ms = (perf_counter() - started) * 1000
        history = ConversationHistoryService(factory, semantic_index=index)
        context = ToolContext(session_id=sid)
        results, times, warm = [], [], []
        for i, (_, query) in enumerate(CASES):
            started = perf_counter()
            result = history.search(context, query=query, mode="semantic", limit=3)
            times.append((perf_counter() - started) * 1000)
            seqs = [m["seq"] for m in result["messages"]]
            expected = i * 2 + 1
            results.append({"query": query, "expected_seq": expected, "returned_seqs": seqs,
                            "top1": seqs[:1] == [expected], "top3": expected in seqs})
        for i in range(args.rounds):
            started = perf_counter()
            history.search(context, query=CASES[i % len(CASES)][1], mode="semantic", limit=3)
            warm.append((perf_counter() - started) * 1000)
        report = {
            "synthetic_only": True, "model": args.model_name, "cases": results,
            "top1_hits": sum(r["top1"] for r in results), "top3_hits": sum(r["top3"] for r in results),
            "index_ms_including_model_load": round(index_ms, 2),
            "new_query_median_ms": round(statistics.median(times), 2),
            "cached_query_median_ms": round(statistics.median(warm), 2),
            "cached_query_max_ms": round(max(warm), 2),
        }
        encoded = json.dumps(report, ensure_ascii=False, indent=2)
        if args.output:
            args.output.write_text(encoded + "\n", encoding="utf-8")
        print(encoded)


if __name__ == "__main__":
    main()
