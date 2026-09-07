"""Local checkpoint lifecycle for LangGraph Agent runs."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver


class SqliteCheckpointRuntime:
    """Open short-lived savers over one durable local checkpoint database."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path

    @asynccontextmanager
    async def async_saver(self) -> AsyncIterator[AsyncSqliteSaver]:
        async with AsyncSqliteSaver.from_conn_string(str(self.path)) as saver:
            await saver.setup()
            yield saver

    @contextmanager
    def sync_saver(self) -> Iterator[SqliteSaver]:
        with SqliteSaver.from_conn_string(str(self.path)) as saver:
            saver.setup()
            yield saver

    def close(self) -> None:
        """Connections are scoped to individual operations; nothing stays open."""


def create_sqlite_checkpoint_runtime(path: Path) -> SqliteCheckpointRuntime:
    return SqliteCheckpointRuntime(path)
