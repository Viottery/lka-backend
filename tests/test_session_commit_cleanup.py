"""Session transaction cleanup preserves uncertain MySQL commit failures."""
from __future__ import annotations

import pytest

from app.core.sessions import SessionService
from app.storage.mysql import MySQLCommitOutcomeUnknown


class _Result:
    def __init__(self, row=None):
        self._row = row
        self.rowcount = 1

    def fetchone(self):
        return self._row


class _CommittedButDisconnected:
    dialect = "mysql"

    def __init__(self):
        self.committed = False
        self.commit_calls = 0
        self.rollback_calls = 0
        self.close_calls = 0
        self.message_inserts = 0

    def execute(self, sql, _params=()):
        if "INSERT OR IGNORE INTO agent_session_messages" in sql:
            self.message_inserts += 1
        return _Result()

    def execute_native(self, _sql, _params=()):
        return _Result({"status": "active"})

    def commit(self):
        self.commit_calls += 1
        self.committed = True
        raise MySQLCommitOutcomeUnknown("private connection detail", errno=2013, sqlstate="HY000")

    def rollback(self):
        self.rollback_calls += 1
        raise RuntimeError("connection already closed")

    def close(self):
        self.close_calls += 1


def test_append_message_preserves_unknown_commit_after_closed_lease_without_retry(monkeypatch):
    connection = _CommittedButDisconnected()
    service = SessionService(lambda: connection)
    monkeypatch.setattr(service, "ensure_session", lambda **_kwargs: None)

    with pytest.raises(MySQLCommitOutcomeUnknown) as caught:
        service.append_message(
            session_id="session-1", role="agent", content="answer", message_id="message-1",
        )

    error = caught.value
    assert error.error_category == "mysql_commit_outcome_unknown"
    assert connection.committed is True
    assert connection.message_inserts == 1
    assert connection.commit_calls == 1
    assert connection.rollback_calls == 1
    assert connection.close_calls == 1
    assert len(error.__notes__) == 1
    assert "rollback cleanup failed (RuntimeError)" in error.__notes__[0]
    assert "connection already closed" not in error.__notes__[0]
