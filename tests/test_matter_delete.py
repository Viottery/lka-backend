import pytest

from app.domains.matters import (
    MatterConflictError,
    MatterCreateInput,
    MatterPlanNotEmptyError,
    MatterService,
)
from app.storage.db import connect, init_db
from tests.test_matter_planning import call
from tests.test_matter_planning import planning_app as _planning_app

planning_app = _planning_app


@pytest.fixture
def service(tmp_path):
    path = tmp_path / "delete.sqlite3"
    init_db(path)
    return MatterService(lambda: connect(path))


def test_soft_delete_hides_matter_and_restore_brings_it_back(service):
    row = service.create_matter(MatterCreateInput(title="Buy printer ink"))
    deleted = service.delete_matter(matter_id=row.matter_id, expected_revision=row.revision)
    assert deleted.deleted_at and deleted.revision == row.revision + 1
    assert service.list_matters().matters == []
    assert service.search_matters(query="printer").matters == []
    with pytest.raises(KeyError):
        service.get_matter(matter_id=row.matter_id)
    # Caches learn about the deletion through the sync feed.
    change = service.sync_matters().changes[-1]
    assert change.matter.matter_id == row.matter_id and change.matter.deleted_at
    restored = service.restore_matter(matter_id=row.matter_id)
    assert restored.deleted_at is None and restored.revision == deleted.revision + 1
    assert [m.matter_id for m in service.search_matters(query="printer").matters] == [row.matter_id]


def test_delete_checks_revision(service):
    row = service.create_matter(MatterCreateInput(title="Stale"))
    with pytest.raises(MatterConflictError):
        service.delete_matter(matter_id=row.matter_id, expected_revision=row.revision + 5)


def test_plan_with_tasks_cannot_be_deleted(service):
    plan = service.create_matter(MatterCreateInput(title="Move house", kind="plan"))
    task = service.create_matter(MatterCreateInput(title="Pack books", parent_matter_id=plan.matter_id))
    with pytest.raises(MatterPlanNotEmptyError):
        service.delete_matter(matter_id=plan.matter_id)
    service.delete_matter(matter_id=task.matter_id)
    assert service.get_matter(matter_id=plan.matter_id).progress["total"] == 0
    service.delete_matter(matter_id=plan.matter_id)
    # A task restored after its plan is gone becomes standalone.
    assert service.restore_matter(matter_id=task.matter_id).parent_matter_id is None


def test_delete_and_restore_http_routes(planning_app):
    app, _ = planning_app
    row = app.state.runtime.matter_service.create_matter(MatterCreateInput(title="HTTP delete"))
    response = call(app, "DELETE", f"/matters/{row.matter_id}", params={"expected_revision": row.revision})
    assert response.status_code == 200 and response.json()["deleted_at"]
    assert call(app, "GET", f"/matters/{row.matter_id}").status_code == 404
    assert call(app, "DELETE", f"/matters/{row.matter_id}").status_code == 404
    restored = call(app, "POST", f"/matters/{row.matter_id}/restore")
    assert restored.status_code == 200 and restored.json()["deleted_at"] is None
    plan = app.state.runtime.matter_service.create_matter(MatterCreateInput(title="P", kind="plan"))
    app.state.runtime.matter_service.create_matter(MatterCreateInput(title="T", parent_matter_id=plan.matter_id))
    blocked = call(app, "DELETE", f"/matters/{plan.matter_id}")
    assert blocked.status_code == 409 and blocked.json()["detail"] == "matter_plan_not_empty"
