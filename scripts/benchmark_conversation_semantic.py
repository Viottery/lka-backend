"""Mechanical benchmark for synthetic conversation semantic indexing/search.

This measures SQLite, serialization, cache and vector math only. It is not a
measurement of embedding-model latency or semantic retrieval quality.
"""

from __future__ import annotations

import argparse
import math
import sqlite3
import statistics
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path

from app.domains.conversation_semantic import ConversationSemanticIndex


class SyntheticProvider:
    model_info = type("ModelInfo", (), {"index_key": "synthetic-384-v1"})()

    def __init__(self, dimensions: int = 384) -> None:
        self.dimensions = dimensions

    def _vector(self, text: str) -> list[float]:
        seed = sum(text.encode("utf-8"))
        values = [math.sin((seed + 1) * (i + 1)) for i in range(self.dimensions)]
        norm = math.sqrt(sum(value * value for value in values))
        return [value / norm for value in values]

    def embed_passages(self, passages: Sequence[str]) -> list[list[float]]:
        return [self._vector(text) for text in passages]

    def embed_query(self, query: str) -> list[float]:
        return self._vector(query)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=10000)
    parser.add_argument("--dimensions", type=int, default=384)
    parser.add_argument("--batch-size", type=int, default=128)
    parser.add_argument("--rounds", type=int, default=30)
    args = parser.parse_args()
    if not 1 <= args.rounds <= 100 or not 1 <= args.rows <= 100000 or not 1 <= args.dimensions <= 4096:
        parser.error("rows=1..100000, dimensions=1..4096, rounds=1..100 required")

    with tempfile.TemporaryDirectory(prefix="conversation-semantic-bench-") as temp_dir:
        database = Path(temp_dir) / "benchmark.sqlite"

        def connect() -> sqlite3.Connection:
            connection = sqlite3.connect(database)
            connection.row_factory = sqlite3.Row
            return connection

        with connect() as conn:
            conn.executescript("""
                CREATE TABLE agent_sessions(session_id TEXT PRIMARY KEY,kind TEXT,status TEXT);
                CREATE TABLE agent_session_context_messages(
                  session_id TEXT,seq INTEGER,role TEXT,content TEXT,created_at TEXT,
                  trace_id TEXT,PRIMARY KEY(session_id,seq));
                INSERT INTO agent_sessions VALUES('benchmark','ordinary','active');
            """)
            conn.executemany(
                "INSERT INTO agent_session_context_messages VALUES(?,?,?,?,?,?)",
                (("benchmark", seq, "user", f"Synthetic passage {seq}", "now", f"t{seq}")
                 for seq in range(1, args.rows + 1)),
            )

        provider = SyntheticProvider(args.dimensions)
        index = ConversationSemanticIndex(connect, provider,
                                          batch_size=args.batch_size,
                                          max_candidates=args.rows)
        start = time.perf_counter()
        batches = 0
        status = index.sync_batch("benchmark")
        batches += 1
        while status["pending"]:
            status = index.sync_batch("benchmark")
            batches += 1
        cold_seconds = time.perf_counter() - start

        index.search("benchmark", "synthetic warm query")  # matrix/query cold
        warm_times = []
        for _ in range(args.rounds):
            start = time.perf_counter()
            index.search("benchmark", "synthetic warm query")
            warm_times.append(time.perf_counter() - start)
        warm_seconds = statistics.median(warm_times)
        warm_p95 = sorted(warm_times)[math.ceil(len(warm_times) * 0.95) - 1]
        print(f"synthetic rows={args.rows} dimensions={args.dimensions} batches={batches}")
        print(f"cold_index_seconds={cold_seconds:.3f} warm_cached_search_median_seconds={warm_seconds:.6f} "
              f"warm_cached_search_p95_seconds={warm_p95:.6f} rounds={args.rounds}")
        print("Mechanical synthetic benchmark only; no real model latency or semantic quality measured.")


if __name__ == "__main__":
    main()
