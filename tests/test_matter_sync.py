from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from app.domains.matters import (
    MatterConflictError,
    MatterCreateInput,
    MatterService,
    MatterSourceLinkInput,
    MatterUpdateInput,
)
from app.storage.db import connect, init_db
from tests.test_matter_planning import call
from tests.test_matter_planning import planning_app as _planning_app
from tests.test_message_matter_proposals import HUMAN, candidate, decision
from tests.test_message_matter_proposals import services as _proposal_services

planning_app = _planning_app
services = _proposal_services


@pytest.fixture
def matter_service(tmp_path):
    path = tmp_path / "sync.sqlite3"
    init_db(path)
    return MatterService(lambda: connect(path))


def all_changes(service, *, after=0, limit=2):
    changes = []
    while True:
        page = service.sync_matters(after=after, limit=limit)
        changes.extend(page.changes)
        after = page.cursor
        if not page.has_more:
            return changes, after


def test_migration_bootstrap_preserves_deadlines_and_legacy_classification(tmp_path):
    path = tmp_path / "old.sqlite3"
    init_db(path)
    conn = connect(path)
    conn.execute(
        "INSERT INTO matters(matter_id,title,summary,status,priority,due_at,tags,metadata,created_at,updated_at) "
        "VALUES('old','Existing','Keep','open','normal','2026-10-19','[]','{}','then','then')"
    )
    conn.commit()
    conn.close()
    for _ in range(2):
        service = MatterService(lambda: connect(path))
        changes, cursor = all_changes(service, limit=1)
        assert len(changes) == 1 and changes[0].sequence == cursor == 1
        row = changes[0].matter
        assert row.matter_id == "old" and row.due_at == "2026-10-19"
        assert (
            row.scheduled_on is None and row.scheduled_start is None and row.scheduled_end is None
        )
        assert (
            row.inbox is False and row.section == "" and row.sections == [] and row.sort_order == 0
        )
        assert row.revision == 1 and row.updated_at == "then"


def test_sync_pages_source_links_and_current_records(matter_service):
    service = matter_service
    task = service.create_matter(MatterCreateInput(title="First", inbox=True))
    second = service.create_matter(MatterCreateInput(title="Second"))
    service.update_matter(
        matter_id=task.matter_id, payload=MatterUpdateInput(status="cancelled", expected_revision=1)
    )
    service.link_source(
        matter_id=task.matter_id,
        source_link=MatterSourceLinkInput(
            source_type="trace", source_id="trace-1", reason="Initial"
        ),
    )
    service.link_source(
        matter_id=task.matter_id,
        source_link=MatterSourceLinkInput(
            source_type="trace", source_id="trace-1", reason="Revised"
        ),
    )
    changes, cursor = all_changes(service, limit=1)
    assert len(changes) > 5  # Link changes and matter revisions both journal transactionally.
    sequences = [change.sequence for change in changes]
    assert sequences == sorted(set(sequences)) and cursor == sequences[-1]
    assert {change.matter.matter_id for change in changes} == {task.matter_id, second.matter_id}
    for change in changes:
        if change.matter.matter_id == task.matter_id:
            assert change.matter.status == "cancelled" and change.matter.revision == 4
            assert change.matter.source_links[0].reason == "Revised"
    empty = service.sync_matters(after=cursor, limit=1)
    assert empty.changes == [] and empty.cursor == cursor and not empty.has_more
    later = service.update_matter(
        matter_id=second.matter_id, payload=MatterUpdateInput(scheduled_on="2026-10-20")
    )
    page = service.sync_matters(after=cursor)
    assert len(page.changes) == 1 and page.changes[0].matter == later


def test_journal_follows_caller_transaction_visibility_and_rollback(matter_service):
    service = matter_service
    conn = service._conn_factory()
    try:
        conn.execute("BEGIN IMMEDIATE")
        pending = service.create_matter(
            MatterCreateInput(
                title="Uncommitted",
                source_links=[MatterSourceLinkInput(source_type="trace", source_id="pending")],
            ),
            conn=conn,
        )
        assert service.sync_matters().changes == []
        conn.rollback()
        assert service.sync_matters().changes == []
        with pytest.raises(KeyError):
            service.get_matter(matter_id=pending.matter_id)
        conn.execute("BEGIN IMMEDIATE")
        committed = service.create_matter(MatterCreateInput(title="Committed"), conn=conn)
        conn.commit()
        page = service.sync_matters()
        assert len(page.changes) == 1 and page.changes[0].matter.matter_id == committed.matter_id
    finally:
        conn.close()


def test_concurrent_revision_writers_emit_only_committed_winner(matter_service):
    service = matter_service
    task = service.create_matter(MatterCreateInput(title="Concurrent"))
    cursor = service.sync_matters().cursor

    def update(title):
        try:
            return service.update_matter(
                matter_id=task.matter_id,
                payload=MatterUpdateInput(title=title, expected_revision=1),
            )
        except MatterConflictError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(update, ["Writer A", "Writer B"]))
    assert sum(result is not None for result in results) == 1
    page = service.sync_matters(after=cursor)
    assert len(page.changes) == 1 and page.changes[0].sequence > cursor
    assert page.changes[0].matter.revision == 2
    assert page.changes[0].matter.title in {"Writer A", "Writer B"}


def test_message_approval_journals_full_record_and_rejected_writes_emit_nothing(services):
    _, matters, proposals = services
    preview = candidate(services)
    assert matters.sync_matters().changes == []
    receipt = proposals.decide(preview["proposal_id"], decision(preview), principal=HUMAN)
    page = matters.sync_matters()
    assert page.changes and {change.matter.matter_id for change in page.changes} == {
        receipt["matter_id"]
    }
    assert all(
        change.matter.source_links[0].source_type == "message_history_message"
        for change in page.changes
    )
    with pytest.raises(PermissionError):
        matters.update_matter(
            matter_id=receipt["matter_id"],
            payload=MatterUpdateInput(scheduled_on="2026-10-20", inbox=True),
        )
    assert matters.sync_matters(after=page.cursor).changes == []
    # Replaying the human decision is idempotent, including its change journal.
    proposals.decide(preview["proposal_id"], decision(preview), principal=HUMAN)
    assert matters.sync_matters(after=page.cursor).changes == []


def test_sync_http_route_and_bounded_parameters(planning_app):
    app, _ = planning_app
    service = app.state.runtime.matter_service
    first = service.create_matter(MatterCreateInput(title="HTTP sync", inbox=True))
    service.create_matter(MatterCreateInput(title="Next"))
    response = call(app, "GET", "/matters/sync", params={"after": 0, "limit": 1})
    assert response.status_code == 200
    page = response.json()
    assert page["has_more"] and len(page["changes"]) == 1
    assert page["changes"][0]["matter"]["matter_id"] == first.matter_id
    assert page["changes"][0]["matter"]["inbox"] is True
    next_page = call(
        app, "GET", "/matters/sync", params={"after": page["cursor"], "limit": 1}
    ).json()
    assert not next_page["has_more"] and next_page["cursor"] > page["cursor"]
    for params in ({"after": -1}, {"limit": 0}, {"limit": 501}):
        assert call(app, "GET", "/matters/sync", params=params).status_code == 422


def test_changes_committed_between_pages_are_not_skipped(matter_service):
    service = matter_service
    first = service.create_matter(MatterCreateInput(title="Initial first"))
    second = service.create_matter(MatterCreateInput(title="Initial second"))
    initial = service.sync_matters(limit=1)
    assert initial.has_more and initial.changes[0].matter.matter_id == first.matter_id
    updated = service.update_matter(
        matter_id=first.matter_id, payload=MatterUpdateInput(scheduled_on="2026-10-21")
    )
    third = service.create_matter(MatterCreateInput(title="Added while paging"))
    rest, cursor = all_changes(service, after=initial.cursor, limit=1)
    cache = {change.matter.matter_id: change.matter for change in initial.changes + rest}
    assert set(cache) == {first.matter_id, second.matter_id, third.matter_id}
    assert cache[first.matter_id] == updated
    assert cursor == rest[-1].sequence


def test_sync_hydration_and_journal_use_same_snapshot(matter_service, monkeypatch):
    service = matter_service
    task = service.create_matter(MatterCreateInput(title="Before concurrent commit"))
    original_get = service.get_matter
    wrote = False

    def get_while_writer_commits(**kwargs):
        nonlocal wrote
        if not wrote:
            wrote = True
            service.update_matter(
                matter_id=task.matter_id, payload=MatterUpdateInput(title="After concurrent commit")
            )
        return original_get(**kwargs)

    monkeypatch.setattr(service, "get_matter", get_while_writer_commits)
    initial = service.sync_matters()
    assert initial.changes[0].matter.title == "Before concurrent commit"
    assert initial.changes[0].matter.revision == 1
    later = service.sync_matters(after=initial.cursor)
    assert len(later.changes) == 1 and later.changes[0].matter.title == "After concurrent commit"
    assert later.changes[0].matter.revision == 2
