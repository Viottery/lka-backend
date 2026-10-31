"""New memory read projections remain equivalent on the real MySQL dialect."""
import hashlib
import json
from uuid import uuid4

import pytest

from app.core.memory_consolidation import MemoryConsolidator
from app.core.memory_reconciliation import scoped_memories
from app.domains.memory import MemoryInput, MemorySourceInput
from tests.test_mysql_runtime_live import runtime as _mysql_runtime


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    yield from _mysql_runtime.__wrapped__(tmp_path, monkeypatch)


def test_mysql_memory_identity_matches_source_fenced_signature(runtime, tmp_path, monkeypatch):
    memory = runtime.memory_service
    project = memory.resolve_project(tmp_path / ('identity-' + uuid4().hex))
    records = []
    for index in range(10):
        source = memory.register_source(MemorySourceInput(
            source_type='user_message', source_ref='identity-' + uuid4().hex, trusted_source=True,
        ))
        records.append(memory.create(MemoryInput(
            content=f'Synthetic identity fact {index}', source_id=source,
            scope='project', project_id=project, sensitivity='normal', user_confirmed=True,
        )))
    memory.revoke_source(records[0].source_ids[0])
    expected = sorted((record.memory_id, record.version)
                      for record in scoped_memories(memory, 'project', project))
    assert len(expected) == 9
    assert MemoryConsolidator(memory).signature('project', project) == hashlib.sha256(json.dumps(expected).encode()).hexdigest()

    original = memory._connect
    calls = []
    def connect():
        connection = original()
        class ReadProxy:
            def execute(self, sql, *args):
                calls.append(sql.lstrip().split(None, 1)[0].upper())
                return connection.execute(sql, *args)
            def close(self):
                connection.close()
        return ReadProxy()
    monkeypatch.setattr(memory, '_connect', connect)
    identities = memory.active_identities(scope='project', project_id=project)
    assert sorted((identity, version) for identity, version, _ in identities) == expected
    assert calls == ['SELECT']
    calls.clear()
    assert len(memory.list(scope='project', project_id=project)) == 9
    assert calls == ['SELECT', 'SELECT']
