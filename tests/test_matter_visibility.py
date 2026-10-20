"""Visibility is a projection, never source authority or a second matter identity."""
import sqlite3
from contextlib import closing

import pytest
from pydantic import ValidationError

from app.core.tools import ToolContext
from app.domains.matters import (
    MatterConflictError,
    MatterCreateInput,
    MatterService,
    MatterSourceLinkInput,
    MatterUpdateInput,
)
from app.storage.db import connect, init_db
from tests.test_message_matter_proposals import HUMAN, candidate, decision
from tests.test_message_matter_proposals import services as _services

services = _services


@pytest.fixture
def planning_app(tmp_path, monkeypatch):
    from tests.test_matter_planning import planning_app as fixture
    return fixture.__wrapped__(tmp_path, monkeypatch)


def call(*args, **kwargs):
    from tests.test_matter_planning import call as request
    return request(*args, **kwargs)


@pytest.fixture
def service(tmp_path):
    path = tmp_path / "matters.sqlite3"
    init_db(path)
    return MatterService(lambda: connect(path), internal_items_enabled=lambda: True)


def test_legacy_additive_migration_defaults_and_sync(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    init_db(path)
    with closing(connect(path)) as conn, conn:
        conn.execute("INSERT INTO matters(matter_id,title,summary,status,priority,tags,metadata,created_at,updated_at) "
                     "VALUES('legacy','Legacy','Keep','open','normal','[]','{}','old','old')")
    for _ in range(2):
        svc = MatterService(lambda: connect(path))
        row = svc.get_matter(matter_id="legacy")
        assert (row.visibility, row.due_at, row.revision, row.updated_at) == ("user", None, 1, "old")
        page = svc.sync_matters()
        assert len(page.changes) == 1 and page.changes[0].matter.visibility == "user"
        with closing(connect(path)) as conn, pytest.raises(sqlite3.IntegrityError, match="CHECK constraint"):
            conn.execute("UPDATE matters SET visibility='unknown' WHERE matter_id='legacy'")


def test_projection_before_pagination_search_and_full_sync(service):
    internal = service.create_matter(MatterCreateInput(title="needle hidden", visibility="internal", inbox=True))
    first = service.create_matter(MatterCreateInput(title="needle first", inbox=True))
    second = service.create_matter(MatterCreateInput(title="needle second", inbox=True))
    assert internal.due_at is None
    page = service.list_matters(limit=1, inbox=True)
    assert page.total_count == 2 and page.next_offset == 1
    rest = service.list_matters(limit=1, offset=1, inbox=True)
    assert rest.total_count == 2 and rest.next_offset is None
    assert {r.matter_id for r in page.matters + rest.matters} == {first.matter_id, second.matter_id}
    assert [r.matter_id for r in service.list_matters(visibility="internal").matters] == [internal.matter_id]
    assert service.list_matters(visibility="all").total_count == 3
    for query in ("needle", ""):
        assert {r.matter_id for r in service.search_matters(query=query).matters} == {first.matter_id, second.matter_id}
        assert [r.visibility for r in service.search_matters(query=query, visibility="internal", limit=1).matters] == ["internal"]
    assert {c.matter.matter_id for c in service.sync_matters().changes} == {internal.matter_id, first.matter_id, second.matter_id}
    assert service.get_matter(matter_id=internal.matter_id).visibility == "internal"


def test_feature_flag_changes_visibility_without_changing_identity_or_dates(service):
    user = service.create_matter(MatterCreateInput(title="User", due_at="2026-11-01", metadata={"internal": True}))
    assert user.visibility == "user"
    cursor = service.sync_matters().cursor
    hidden = service.update_matter(matter_id=user.matter_id, payload=MatterUpdateInput(visibility="internal", expected_revision=1))
    assert hidden.revision == 2 and hidden.due_at == user.due_at
    assert service.list_matters().total_count == 0
    assert service.sync_matters(after=cursor).changes[-1].matter.visibility == "internal"
    service.internal_items_enabled = lambda: False
    with pytest.raises(PermissionError, match="disabled"):
        service.create_matter(MatterCreateInput(title="Blocked", visibility="internal"))
    # Disabling creation never prevents editing or recovering existing hidden work.
    edited = service.update_matter(matter_id=user.matter_id, payload=MatterUpdateInput(summary="Keep working"))
    assert edited.visibility == "internal"
    shown = service.update_matter(matter_id=user.matter_id, payload=MatterUpdateInput(visibility="user", expected_revision=edited.revision))
    assert shown.matter_id == user.matter_id and shown.due_at == user.due_at
    assert service.list_matters().total_count == 1
    with pytest.raises(PermissionError, match="disabled"):
        service.update_matter(matter_id=user.matter_id, payload=MatterUpdateInput(visibility="internal", expected_revision=shown.revision))
    with pytest.raises(MatterConflictError):
        service.update_matter(matter_id=user.matter_id, payload=MatterUpdateInput(visibility="internal", expected_revision=1))
    assert service.get_matter(matter_id=user.matter_id).revision == shown.revision


def test_internal_disabled_by_default_and_metadata_does_not_enable(tmp_path):
    path = tmp_path / "off.sqlite3"
    init_db(path)
    svc = MatterService(lambda: connect(path))
    with pytest.raises(PermissionError):
        svc.create_matter(MatterCreateInput(title="No", visibility="internal", metadata={"internal_items_enabled": True}))


@pytest.mark.parametrize("model,fields", [(MatterCreateInput, {"title": "Bad"}), (MatterUpdateInput, {})])
@pytest.mark.parametrize("visibility", ["all", "hidden", None])
def test_unknown_write_visibility_rejected(model, fields, visibility):
    with pytest.raises(ValidationError):
        model(**fields, visibility=visibility)


def test_unknown_read_visibility_rejected(service):
    with pytest.raises(ValueError, match="visibility"):
        service.list_matters(visibility="hidden")
    with pytest.raises(ValueError, match="visibility"):
        service.search_matters(query="", visibility="hidden")


def test_plan_progress_user_only_even_internal_expanded_cancelled_parent(service):
    plan = service.create_matter(MatterCreateInput(title="Plan", kind="plan", status="cancelled"))
    for status, visibility in [("open", "user"), ("done", "user"), ("cancelled", "user"), ("done", "internal")]:
        service.create_matter(MatterCreateInput(title=status, status=status, visibility=visibility, parent_matter_id=plan.matter_id))
    expected = {"total": 3, "done": 1, "cancelled": 1}
    assert service.get_matter(matter_id=plan.matter_id).progress == expected
    assert service.list_matters(kind="plan", visibility="all").matters[0].progress == expected
    assert service.list_matters(parent_matter_id=plan.matter_id, visibility="all").total_count == 4
    assert service.get_matter(matter_id=plan.matter_id).status == "cancelled"
    with pytest.raises(ValueError, match="parent must be a plan"):
        task = service.create_matter(MatterCreateInput(title="Task"))
        service.create_matter(MatterCreateInput(title="Nested", visibility="internal", parent_matter_id=task.matter_id))


def test_visibility_never_expands_source_scope_and_progress(service):
    plan = service.create_matter(MatterCreateInput(title="Plan", kind="plan"))
    allowed = service.create_matter(MatterCreateInput(title="needle allowed", parent_matter_id=plan.matter_id))
    hidden = service.create_matter(MatterCreateInput(title="needle internal", visibility="internal", parent_matter_id=plan.matter_id))
    restricted = service.create_matter(MatterCreateInput(title="needle source", visibility="internal", parent_matter_id=plan.matter_id,
                                                       source_links=[MatterSourceLinkInput(source_type="local_evidence", source_id="private")]))
    scope = {"source_ids": (), "account_ids": (), "allow_unattributed": True}
    page = service.list_matters(kind="task", visibility="all", limit=1, **scope)
    assert page.total_count == 2 and page.next_offset == 1
    assert {r.matter_id for r in service.search_matters(query="needle", visibility="all", **scope).matters} == {allowed.matter_id, hidden.matter_id}
    assert service.get_matter(matter_id=plan.matter_id, **scope).progress == {"total": 1, "done": 0, "cancelled": 0}
    with pytest.raises(PermissionError):
        service.get_matter(matter_id=restricted.matter_id, **scope)
    with pytest.raises(PermissionError):
        service.update_matter(matter_id=restricted.matter_id, payload=MatterUpdateInput(visibility="user"), **scope)


def test_caller_transaction_stable_id_rollback_and_snapshot_reads(service, monkeypatch):
    factory = service._conn_factory
    with closing(factory()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        def no_new_connection():
            raise AssertionError("Must reuse caller transaction")
        monkeypatch.setattr(service, "_conn_factory", no_new_connection)
        created = service.create_matter(MatterCreateInput(title="Stable needle", visibility="internal"), conn=conn, matter_id="matter_stable")
        assert created.matter_id == "matter_stable" and conn.in_transaction
        assert service.get_matter(matter_id=created.matter_id, conn=conn).visibility == "internal"
        assert service.list_matters(visibility="all", conn=conn).total_count == 1
        assert service.search_matters(query="needle", visibility="internal", conn=conn).matters[0].matter_id == created.matter_id
        service.update_matter(matter_id=created.matter_id, payload=MatterUpdateInput(visibility="user", expected_revision=1), conn=conn)
        assert conn.in_transaction
        conn.rollback()
        assert service.list_matters(visibility="all", conn=conn).total_count == 0
        assert conn.execute("SELECT count(*) FROM matter_changes").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM matters_fts").fetchone()[0] == 0


def test_internal_message_sources_still_require_dedicated_receipt(services):
    messages, svc, proposals = services
    svc.internal_items_enabled = lambda: True
    message_id = messages.recent()["messages"][0]["message_id"]
    with pytest.raises(PermissionError, match="receipt_required"):
        svc.create_matter(MatterCreateInput(title="Forged", visibility="internal",
                          source_links=[MatterSourceLinkInput(source_type="message_history_message", source_id=message_id)]))
    preview = candidate(services)
    accepted = proposals.decide(preview["proposal_id"], decision(preview), principal=HUMAN)
    with pytest.raises(PermissionError, match="receipt_required"):
        svc.update_matter(matter_id=accepted["matter_id"], payload=MatterUpdateInput(visibility="internal"))
    assert svc.get_matter(matter_id=accepted["matter_id"]).visibility == "user"


def test_api_and_registered_tools_visibility(planning_app):
    app, _ = planning_app
    runtime = app.state.runtime
    svc = runtime.matter_service
    svc.internal_items_enabled = lambda: True
    created = call(app, "POST", "/matters", json={"title": "needle API", "visibility": "internal", "matter_id": "forged"})
    assert created.status_code == 200, created.text
    record = created.json()
    assert record["visibility"] == "internal" and record["matter_id"] != "forged"
    assert call(app, "GET", "/matters").json()["total_count"] == 0
    assert call(app, "GET", "/matters", params={"visibility": "all"}).json()["total_count"] == 1
    assert call(app, "GET", "/matters/search", params={"q": "needle"}).json()["matters"] == []
    assert call(app, "GET", "/matters/search", params={"q": "needle", "visibility": "internal"}).json()["matters"][0]["visibility"] == "internal"
    assert call(app, "GET", "/matters/sync").json()["changes"][0]["matter"]["visibility"] == "internal"
    assert call(app, "GET", "/matters/" + record["matter_id"]).status_code == 200
    assert call(app, "GET", "/matters", params={"visibility": "hidden"}).status_code == 422
    assert call(app, "POST", "/matters", json={"title": "Invalid", "visibility": "all"}).status_code == 422
    executor = runtime.agent_turn_loop.tool_executor
    context = ToolContext(session_id="test", safety_review_approved=True)
    for name, payload in [("matter.list", {"visibility": "internal"}), ("matter.search", {"query": "needle", "visibility": "internal"})]:
        result = executor.execute(invocation_id=name, tool_name=name, tool_input=payload, context=context)
        assert result.status == "completed", result
        assert result.output["matters"][0]["visibility"] == "internal"
