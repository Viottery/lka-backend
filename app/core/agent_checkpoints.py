"""Local checkpoint lifecycle for LangGraph Agent runs."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, closing, contextmanager
from pathlib import Path

import aiosqlite
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from app.storage.sqlite_timing import connect_sqlite


class SqliteCheckpointRuntime:
    """Open short-lived savers over one durable local checkpoint database."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path

    @asynccontextmanager
    async def async_saver(self) -> AsyncIterator[AsyncSqliteSaver]:
        # Use aiosqlite's connector so opening and operations in its worker are timed.
        async with aiosqlite.Connection(lambda: connect_sqlite(str(self.path)), iter_chunk_size=64) as conn:
            saver = AsyncSqliteSaver(conn)
            await saver.setup()
            yield saver

    @contextmanager
    def sync_saver(self) -> Iterator[SqliteSaver]:
        with closing(connect_sqlite(str(self.path), check_same_thread=False)) as conn:
            saver = SqliteSaver(conn)
            saver.setup()
            yield saver

    def close(self) -> None:
        """Connections are scoped to individual operations; nothing stays open."""


def create_sqlite_checkpoint_runtime(path: Path) -> SqliteCheckpointRuntime:
    return SqliteCheckpointRuntime(path)
