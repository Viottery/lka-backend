from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime

import httpx
import pytest

from app.api.main import create_app
from app.core.config import get_settings
from app.core.tools import ToolContext
from app.domains.matters import (
    MatterCreateInput,
    MatterService,
    MatterSourceLinkInput,
    MatterUpdateInput,
)


@pytest.fixture
def planning_app(tmp_path, monkeypatch):
    workspace = tmp_path / "workspaces"
    workspace.mkdir()
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(tmp_path / "missing.toml"))
    monkeypatch.setenv("LKA_WORKSPACE_ROOTS", str(workspace))
    monkeypatch.delenv("LKA_MEMORY_API_TOKEN", raising=False)
    get_settings.cache_clear()
    return create_app(), workspace


def call(app, method, url, **kwargs):
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 1234)),
            base_url="http://test",
        ) as client:
            return await client.request(method, url, **kwargs)

    return asyncio.run(run())


def create(app, **fields):
    response = call(app, "POST", "/matters", json={"title": "A task", **fields})
    assert response.status_code == 200, response.text
    return response.json()


def test_independent_plan_progress_pagination_and_revision(planning_app):
    app, _ = planning_app
    plan = create(app, kind="plan", title="Prepare launch")
    first = create(app, title="Draft checklist", parent_matter_id=plan["matter_id"])
    create(app, title="Final review", status="done", parent_matter_id=plan["matter_id"])
    create(app, title="Old idea", status="cancelled", parent_matter_id=plan["matter_id"])
    response = call(app, "GET", f"/matters/{plan['matter_id']}")
    assert response.json()["progress"] == {"total": 3, "done": 1, "cancelled": 1}
    page = call(
        app, "GET", "/matters", params={"parent_matter_id": plan["matter_id"], "limit": 1}
    ).json()
    assert page["total_count"] == 3 and page["next_offset"] == 1
    second = call(
        app,
        "GET",
        "/matters",
        params={"parent_matter_id": plan["matter_id"], "limit": 2, "offset": 1},
    ).json()
    assert second["total_count"] == 3 and second["next_offset"] is None
    assert {row["matter_id"] for row in page["matters"] + second["matters"]} == {
        row["matter_id"]
        for row in call(app, "GET", "/matters", params={"kind": "task"}).json()["matters"]
    }
    updated = call(
        app,
        "PATCH",
        f"/matters/{plan['matter_id']}",
        json={"status": "done", "expected_revision": plan["revision"]},
    )
    assert updated.status_code == 200
    assert call(app, "GET", f"/matters/{first['matter_id']}").json()["status"] == "open"
    stale = call(
        app,
        "PATCH",
        f"/matters/{plan['matter_id']}",
        json={"title": "Stale rename", "expected_revision": plan["revision"]},
    )
    assert stale.status_code == 409 and stale.json()["detail"] == "matter_revision_conflict"
    assert call(app, "GET", "/matters/missing").status_code == 404


def test_directory_association_parent_rules_and_detach(planning_app):
    app, workspace = planning_app
    directory = workspace / "project"
    directory.mkdir()
    project = call(app, "POST", "/projects", json={"path": str(directory)}).json()
    plan = create(app, kind="plan", project_id=project["project_id"])
    task = create(app, parent_matter_id=plan["matter_id"])
    assert task["project_id"] == project["project_id"]
    assert (
        call(
            app, "GET", "/matters", params={"project_id": project["project_id"], "kind": "task"}
        ).json()["total_count"]
        == 1
    )
    for fields in (
        {"parent_matter_id": task["matter_id"]},
        {"kind": "plan", "parent_matter_id": plan["matter_id"]},
        {"parent_matter_id": plan["matter_id"], "project_id": None},
        {"project_id": "unknown"},
    ):
        assert (
            call(app, "POST", "/matters", json={"title": "Invalid relation", **fields}).status_code
            == 422
        )
    assert (
        call(app, "PATCH", f"/matters/{plan['matter_id']}", json={"project_id": None}).status_code
        == 422
    )
    detached = call(
        app,
        "PATCH",
        f"/matters/{task['matter_id']}",
        json={"parent_matter_id": None, "project_id": None, "expected_revision": task["revision"]},
    )
    assert detached.status_code == 200
    assert detached.json()["parent_matter_id"] is None and detached.json()["project_id"] is None
    assert (
        call(app, "PATCH", f"/matters/{plan['matter_id']}", json={"project_id": None}).status_code
        == 200
    )
    assert call(app, "GET", f"/matters/{plan['matter_id']}").json()["progress"]["total"] == 0


def test_due_filters_are_chronological_and_deadline_can_be_cleared(planning_app):
    app, _ = planning_app
    before = create(app, title="Overdue", due_at="2026-10-18T23:59:00+08:00")
    early = create(app, title="Today earliest", due_at="2026-10-19T01:00:00+08:00")
    later = create(app, title="Today later", due_at="2026-10-18T18:00:00Z", status="waiting")
    create(app, title="Tomorrow", due_at="2026-10-20T00:00:00+08:00")
    create(app, title="Finished", due_at="2026-10-19", status="done")
    date_only = create(app, title="Calendar day", due_at="2026-10-19")
    no_deadline = create(app, title="No deadline")
    # Legacy invalid values remain readable and sort last without entering dated views.
    conn = app.state.runtime._conn()
    try:
        conn.execute(
            "UPDATE matters SET due_at='legacy-invalid-date' WHERE matter_id=?",
            (no_deadline["matter_id"],),
        )
        conn.commit()
    finally:
        conn.close()
    unscheduled = create(app, title="Unscheduled")
    assert (
        call(app, "GET", "/matters", params={"unscheduled": True}).json()["matters"][0]["matter_id"]
        == unscheduled["matter_id"]
    )
    assert call(app, "GET", "/matters").status_code == 200
    page = call(
        app,
        "GET",
        "/matters",
        params={
            "status": "active",
            "due_after": "2026-10-19T00:00:00+08:00",
            "due_before": "2026-10-20T00:00:00+08:00",
        },
    ).json()
    assert [row["matter_id"] for row in page["matters"]] == [
        early["matter_id"],
        later["matter_id"],
        date_only["matter_id"],
    ]
    overdue = call(
        app, "GET", "/matters", params={"due_before": "2026-10-19T00:00:00+08:00"}
    ).json()
    assert [row["matter_id"] for row in overdue["matters"]] == [before["matter_id"]]
    unchanged = call(
        app, "PATCH", f"/matters/{early['matter_id']}", json={"summary": "Updated note"}
    ).json()
    assert unchanged["due_at"] == early["due_at"]
    cleared = call(app, "PATCH", f"/matters/{early['matter_id']}", json={"due_at": None}).json()
    assert cleared["due_at"] is None
    for bad in ("tomorrow", "2026-02-30", "2026-10-19T12:00:00"):
        assert (
            call(app, "POST", "/matters", json={"title": "Bad deadline", "due_at": bad}).status_code
            == 422
        )
        assert (
            call(app, "PATCH", f"/matters/{early['matter_id']}", json={"due_at": bad}).status_code
            == 422
        )
        assert call(app, "GET", "/matters", params={"due_after": bad}).status_code == 422


@pytest.mark.parametrize("offset", ["-07:00", "+08:00"])
def test_calendar_deadlines_use_local_day_but_timed_deadlines_use_instants(planning_app, offset):
    app, _ = planning_app
    service = app.state.runtime.matter_service

    def add(title, due_at):
        return service.create_matter(MatterCreateInput(title=title, due_at=due_at)).matter_id

    yesterday = add("Yesterday calendar", "2026-10-18")
    today = add("Today calendar", "2026-10-19")
    tomorrow = add("Tomorrow calendar", "2026-10-20")
    timed_before = add("Before local midnight", f"2026-10-18T23:59:59{offset}")
    timed_start = add("At local midnight", f"2026-10-19T00:00:00{offset}")
    timed_end = add("At next local midnight", f"2026-10-20T00:00:00{offset}")
    start = f"2026-10-19T00:00:00{offset}"
    end = f"2026-10-20T00:00:00{offset}"
    page = call(app, "GET", "/matters", params={"due_after": start, "due_before": end}).json()
    assert {row["matter_id"] for row in page["matters"]} == {today, timed_start}
    overdue = call(app, "GET", "/matters", params={"due_before": start}).json()
    assert {row["matter_id"] for row in overdue["matters"]} == {yesterday, timed_before}
    assert today not in {row["matter_id"] for row in overdue["matters"]}
    upcoming = service.list_matters(due_after=end)
    assert {row.matter_id for row in upcoming.matters} == {tomorrow, timed_end}
    # Equivalent UTC instants preserve exact timed boundary membership.
    utc_start = datetime.fromisoformat(start).astimezone(UTC).isoformat()
    timed = service.list_matters(due_after=utc_start)
    assert timed_start in {row.matter_id for row in timed.matters}
    assert timed_before not in {row.matter_id for row in timed.matters}


def test_literal_search_and_fts_structural_filters(planning_app):
    app, _ = planning_app
    plan = create(app, kind="plan", title="Release 100%_ready")
    child = create(app, parent_matter_id=plan["matter_id"], summary="100%_ready release notes")
    create(app, title="Release 100Xready")
    literal = call(app, "GET", "/matters", params={"q": "%_", "kind": "task"}).json()
    assert [row["matter_id"] for row in literal["matters"]] == [child["matter_id"]]
    fts = call(
        app,
        "GET",
        "/matters/search",
        params={"q": "release", "parent_matter_id": plan["matter_id"], "kind": "task"},
    ).json()
    assert [row["matter_id"] for row in fts["matters"]] == [child["matter_id"]]
    assert fts["matters"][0]["parent_matter_id"] == plan["matter_id"]


def test_tools_create_read_list_and_update_plans(planning_app):
    app, _ = planning_app
    executor = app.state.runtime.tool_executor
    context = ToolContext(
        session_id="planning",
        trace_id="planning",
        context_id="planning",
        safety_review_approved=True,
        safety_review_id="test-planning-review",
    )

    def execute(name, data):
        result = executor.execute(
            invocation_id=name, tool_name=name, tool_input=data, context=context
        )
        assert result.status == "completed", result
        return result.output

    plan = execute(
        "matter.create", {"kind": "plan", "title": "Study plan", "summary": "Prepare for exam"}
    )["matter"]
    task = execute(
        "matter.create",
        {
            "title": "Read chapter",
            "summary": "Chapter one",
            "parent_matter_id": plan["matter_id"],
            "due_at": "2026-10-19",
        },
    )["matter"]
    assert (
        execute("matter.get", {"matter_id": plan["matter_id"]})["matter"]["progress"]["total"] == 1
    )
    listed = execute(
        "matter.list",
        {
            "kind": "task",
            "parent_matter_id": plan["matter_id"],
            "status": "active",
            "q": "chapter",
            "offset": 0,
        },
    )
    assert listed["total_count"] == 1 and listed["matters"][0]["matter_id"] == task["matter_id"]
    updated = execute(
        "matter.update",
        {
            "matter_id": task["matter_id"],
            "expected_revision": task["revision"],
            "status": "done",
            "due_at": None,
        },
    )["matter"]
    assert updated["due_at"] is None and updated["status"] == "done"
    assert (
        execute("matter.get", {"matter_id": plan["matter_id"]})["matter"]["progress"]["done"] == 1
    )
    stale = executor.execute(
        invocation_id="stale",
        tool_name="matter.update",
        tool_input={
            "matter_id": task["matter_id"],
            "expected_revision": task["revision"],
            "title": "Stale",
        },
        context=context,
    )
    assert stale.status != "completed"


def test_child_scope_filters_before_paging_progress_and_parent_links(planning_app):
    app, _ = planning_app
    service = app.state.runtime.matter_service
    # Full-data children may inspect unattributed plans, but restricted children cannot.
    plan = service.create_matter(MatterCreateInput(kind="plan", title="Private plan"))
    first = service.create_matter(
        MatterCreateInput(title="Unattributed task", parent_matter_id=plan.matter_id)
    )
    hidden = service.create_matter(
        MatterCreateInput(
            title="Unverifiable evidence",
            status="done",
            parent_matter_id=plan.matter_id,
            source_links=[MatterSourceLinkInput(source_type="local_evidence", source_id="private")],
        )
    )
    scope = {"source_ids": (), "account_ids": (), "allow_unattributed": True}
    page = service.list_matters(kind="task", parent_matter_id=plan.matter_id, limit=1, **scope)
    assert page.total_count == 1 and page.next_offset is None
    assert page.matters[0].matter_id == first.matter_id
    assert service.get_matter(matter_id=plan.matter_id, **scope).progress == {
        "total": 1,
        "done": 0,
        "cancelled": 0,
    }
    with pytest.raises(PermissionError):
        service.get_matter(matter_id=hidden.matter_id, **scope)
    hidden_plan = service.create_matter(
        MatterCreateInput(
            kind="plan",
            title="Restricted plan",
            source_links=[
                MatterSourceLinkInput(source_type="local_evidence", source_id="private-plan")
            ],
        )
    )
    with pytest.raises(PermissionError):
        service.update_matter(
            matter_id=first.matter_id,
            payload=MatterUpdateInput(parent_matter_id=hidden_plan.matter_id),
            **scope,
        )
    assert service.get_matter(matter_id=first.matter_id).parent_matter_id == plan.matter_id
    blocked = {**scope, "allow_unattributed": False}
    with pytest.raises(PermissionError):
        service.update_matter(
            matter_id=first.matter_id,
            payload=MatterUpdateInput(parent_matter_id=plan.matter_id),
            **blocked,
        )
    context = ToolContext(
        session_id="child",
        trace_id="child",
        context_id="child",
        tool_view={
            "snapshot_id": "snapshot-child",
            "child_run_id": "child",
            "side_effect_level": "read",
            "allowed_packages": ("matter",),
        },
        safety_review_approved=True,
        safety_review_id="child-review",
    )
    result = app.state.runtime.tool_executor.execute(
        invocation_id="child-get",
        tool_name="matter.get",
        tool_input={"matter_id": plan.matter_id},
        context=context,
    )
    assert result.status != "completed"


def test_existing_message_gate_still_blocks_generic_plan_and_due_edits(planning_app):
    app, _ = planning_app
    task = create(app)
    # Seed an already-approved message link to exercise generic mutation protection.
    conn = app.state.runtime._conn()
    try:
        conn.execute(
            "INSERT INTO matter_source_links(matter_id, source_type, source_id, reason, created_at) VALUES(?, 'message_history_message', 'approved-message', 'Reviewed', 'now')",
            (task["matter_id"],),
        )
        conn.commit()
    finally:
        conn.close()
    for fields in (
        {"due_at": None},
        {"parent_matter_id": None},
        {"status": "done", "expected_revision": task["revision"]},
    ):
        assert call(app, "PATCH", f"/matters/{task['matter_id']}", json=fields).status_code == 403
    assert call(app, "GET", f"/matters/{task['matter_id']}").status_code == 200


def test_legacy_matters_migrate_without_rewriting_records(tmp_path):
    database = tmp_path / "legacy.sqlite"

    def connection():
        conn = sqlite3.connect(database)
        conn.row_factory = sqlite3.Row
        return conn

    conn = connection()
    conn.executescript("""
        CREATE TABLE matters(matter_id TEXT PRIMARY KEY,title TEXT,summary TEXT,status TEXT,priority TEXT,due_at TEXT,tags TEXT,metadata TEXT,created_at TEXT,updated_at TEXT);
        CREATE TABLE matter_source_links(matter_id TEXT,source_type TEXT,source_id TEXT,reason TEXT,created_at TEXT);
        INSERT INTO matters VALUES('legacy','Existing','Keep','open','normal',NULL,'[]','{}','then','then');
    """)
    conn.close()
    for _ in range(2):
        service = MatterService(connection)
        row = service.get_matter(matter_id="legacy")
        assert row.kind == "task" and row.parent_matter_id is None and row.project_id is None
        assert row.title == "Existing" and row.updated_at == "then" and row.revision == 1


def test_planning_schedule_fields_roundtrip_clear_and_keep_deadline(planning_app):
    app, _ = planning_app
    plan = create(
        app, kind="plan", title="Ordered plan", sections=["Preparation", "Empty future stage"]
    )
    task = create(
        app,
        title="Scheduled work",
        parent_matter_id=plan["matter_id"],
        scheduled_on="2026-10-20",
        scheduled_start="2026-10-20T09:00:00+08:00",
        scheduled_end="2026-10-20T10:00:00+08:00",
        due_at="2026-10-22",
        inbox=True,
        section="Preparation",
        sort_order=-1.5,
    )
    assert task["scheduled_on"] == "2026-10-20" and task["due_at"] == "2026-10-22"
    assert task["inbox"] is True and task["sort_order"] == -1.5 and task["section"] == "Preparation"
    assert call(app, "GET", f"/matters/{plan['matter_id']}").json()["sections"] == [
        "Preparation",
        "Empty future stage",
    ]
    # Editing notes preserves all scheduling state; explicit null removes only planning.
    retained = call(
        app, "PATCH", f"/matters/{task['matter_id']}", json={"summary": "Updated note"}
    ).json()
    assert retained["scheduled_start"] == task["scheduled_start"] and retained["inbox"] is True
    cleared = call(
        app,
        "PATCH",
        f"/matters/{task['matter_id']}",
        json={
            "expected_revision": retained["revision"],
            "scheduled_on": None,
            "scheduled_start": None,
            "scheduled_end": None,
            "inbox": False,
            "section": "",
            "sort_order": 0,
        },
    )
    assert cleared.status_code == 200, cleared.text
    row = cleared.json()
    assert (
        row["due_at"] == "2026-10-22"
        and row["scheduled_on"] is None
        and row["scheduled_start"] is None
        and row["scheduled_end"] is None
    )
    assert row["inbox"] is False and row["section"] == "" and row["sort_order"] == 0
    stages = call(app, "PATCH", f"/matters/{plan['matter_id']}", json={"sections": []})
    assert stages.status_code == 200 and stages.json()["sections"] == []


def test_planning_schedule_validation_and_source_review(planning_app):
    app, _ = planning_app
    task = create(app)
    invalid = [
        {"scheduled_on": "2026-02-30"},
        {"scheduled_on": "2026-10-20T00:00:00Z"},
        {"scheduled_start": "2026-10-20T09:00:00"},
        {"scheduled_start": "2026-10-20", "scheduled_end": "2026-10-21"},
        {"scheduled_start": "2026-10-20T09:00:00Z"},
        {"scheduled_start": "2026-10-20T09:00:00Z", "scheduled_end": "2026-10-20T09:00:00Z"},
        {"scheduled_start": "2026-10-20T09:00:00Z", "scheduled_end": "2026-10-20T10:00:00+08:00"},
        {"section": "x" * 121},
        {"section": None},
        {"inbox": 1},
        {"inbox": None},
        {"sort_order": "Infinity"},
        {"sort_order": "NaN"},
        {"sections": ["Task cannot define stages"]},
    ]
    for fields in invalid:
        assert (
            call(app, "POST", "/matters", json={"title": "Invalid", **fields}).status_code == 422
        ), fields
        assert (
            call(app, "PATCH", f"/matters/{task['matter_id']}", json=fields).status_code == 422
        ), fields
    for sections in ([""], ["Duplicate", "Duplicate"], ["x" * 121], [str(i) for i in range(101)]):
        assert (
            call(
                app,
                "POST",
                "/matters",
                json={"title": "Plan", "kind": "plan", "sections": sections},
            ).status_code
            == 422
        )
    # Scheduling cannot bypass the existing receipt gate for approved message matters.
    conn = app.state.runtime._conn()
    try:
        conn.execute(
            "INSERT INTO matter_source_links(matter_id,source_type,source_id,reason,created_at) VALUES(?, 'message_history_message','reviewed-message','Reviewed','now')",
            (task["matter_id"],),
        )
        conn.commit()
    finally:
        conn.close()
    assert (
        call(
            app,
            "PATCH",
            f"/matters/{task['matter_id']}",
            json={"scheduled_on": "2026-10-20", "inbox": True},
        ).status_code
        == 403
    )


def test_planning_schedule_filter_tool_parity_and_partial_block_edit(planning_app):
    app, _ = planning_app
    executor = app.state.runtime.tool_executor
    context = ToolContext(
        session_id="schedule",
        trace_id="schedule",
        context_id="schedule",
        safety_review_approved=True,
        safety_review_id="schedule-review",
    )

    def execute(name, data):
        result = executor.execute(
            invocation_id=name, tool_name=name, tool_input=data, context=context
        )
        assert result.status == "completed", result
        return result.output

    plan = execute(
        "matter.create",
        {
            "kind": "plan",
            "title": "Tool plan",
            "summary": "Stages",
            "sections": ["Empty stage", "Work"],
        },
    )["matter"]
    task = execute(
        "matter.create",
        {
            "title": "Tool schedule",
            "summary": "Execution time",
            "parent_matter_id": plan["matter_id"],
            "scheduled_on": "2026-10-20",
            "scheduled_start": "2026-10-20T09:00:00+08:00",
            "scheduled_end": "2026-10-20T10:00:00+08:00",
            "section": "Work",
            "inbox": True,
            "sort_order": -2,
        },
    )["matter"]
    create(app, title="Later", scheduled_on="2026-10-21", section="Work", inbox=True, sort_order=2)
    filters = {
        "scheduled_after": "2026-10-20",
        "scheduled_before": "2026-10-21",
        "inbox": True,
        "section": "Work",
    }
    http_page = call(app, "GET", "/matters", params=filters).json()
    tool_page = execute("matter.list", filters)
    assert http_page == tool_page and tool_page["total_count"] == 1
    assert tool_page["matters"][0]["matter_id"] == task["matter_id"]
    assert execute("matter.list", {"scheduled_on": "2026-10-20"})["total_count"] == 1
    updated = execute(
        "matter.update",
        {
            "matter_id": task["matter_id"],
            "expected_revision": task["revision"],
            "scheduled_end": "2026-10-20T11:00:00+08:00",
            "inbox": False,
        },
    )["matter"]
    assert updated["scheduled_start"] == task["scheduled_start"] and updated[
        "scheduled_end"
    ].endswith("11:00:00+08:00")
    assert not updated["inbox"]
    invalid_clear = call(
        app, "PATCH", f"/matters/{task['matter_id']}", json={"scheduled_start": None}
    )
    assert invalid_clear.status_code == 422
    assert execute("matter.get", {"matter_id": task["matter_id"]})["matter"] == updated
