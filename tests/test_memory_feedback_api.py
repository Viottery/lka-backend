from __future__ import annotations

import asyncio

import httpx

from app.api.main import create_app
from app.core.config import get_settings


def make_app(tmp_path, monkeypatch):
    data = tmp_path / "isolated-data"
    config = tmp_path / "isolated-config.toml"
    workspace = tmp_path / "workspaces"
    workspace.mkdir()
    config.write_text("[memory]\n", encoding="utf-8")
    monkeypatch.setenv("LKA_DATA_DIR", str(data))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(config))
    monkeypatch.setenv("LKA_WORKSPACE_ROOTS", str(workspace))
    monkeypatch.setenv("LKA_MEMORY_API_TOKEN", "test-memory-token")
    get_settings.cache_clear()
    return create_app(), workspace


def call(app, method, url, *, token="test-memory-token", **kwargs):
    headers = kwargs.pop("headers", {})
    if token:
        headers["Authorization"] = f"Bearer {token}"

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 1234)),
            base_url="http://test",
        ) as client:
            return await client.request(method, url, headers=headers, **kwargs)

    return asyncio.run(run())


def create_memory(app, content, *, scope="global", workspace_path=None):
    response = call(app, "POST", "/memories", json={
        "content": content, "memory_type": "preference", "scope": scope,
        "workspace_path": workspace_path,
    })
    assert response.status_code == 201, response.text
    return response.json()


def test_feedback_auth_scope_pagination_and_project_isolation(tmp_path, monkeypatch):
    app, workspace = make_app(tmp_path, monkeypatch)
    a, b = workspace / "a", workspace / "b"
    a.mkdir()
    b.mkdir()
    global_one = create_memory(app, "全局偏好一")
    global_two = create_memory(app, "全局偏好二")
    project_a = create_memory(app, "项目A偏好", scope="project", workspace_path=str(a))
    create_memory(app, "项目B偏好", scope="project", workspace_path=str(b))

    for record, content in ((global_one, "全局偏好一修正"), (global_two, "全局偏好二修正"),
                            (project_a, "项目A偏好修正")):
        response = call(app, "PATCH", f"/memories/{record['memory_id']}", json={
            "content": content, "expected_version": record["version"],
        }, **({"params": {"workspace_path": str(a)}} if record is project_a else {}))
        assert response.status_code == 200, response.text

    assert call(app, "GET", "/memories/feedback", token=None).status_code == 401
    assert call(app, "GET", "/memories/feedback", token="wrong-memory-token").status_code == 401
    page = call(app, "GET", "/memories/feedback?scope=global&limit=1&offset=0").json()
    assert len(page["feedback"]) == 1 and page["next_offset"] == 1
    second = call(app, "GET", f"/memories/feedback?scope=global&limit=1&offset={page['next_offset']}").json()
    assert len(second["feedback"]) == 1 and second["next_offset"] is None
    assert {row["scope"] for row in page["feedback"] + second["feedback"]} == {"global"}

    project_page = call(app, "GET", "/memories/feedback", params={
        "scope": "project", "workspace_path": str(a),
    }).json()
    assert len(project_page["feedback"]) == 1
    assert project_page["feedback"][0]["project_id"] == project_a["project_id"]
    assert call(app, "GET", "/memories/feedback", params={
        "scope": "project", "workspace_path": str(b),
    }).json()["feedback"] == []

    untouched = workspace / "untouched"
    untouched.mkdir()
    assert app.state.runtime.memory_service.resolve_project(untouched, create=False) is None
    assert call(app, "GET", "/memories/feedback", params={
        "scope": "project",
    }).status_code == 422
    outside = tmp_path / "outside"
    outside.mkdir()
    assert call(app, "GET", "/memories/feedback", params={
        "scope": "project", "workspace_path": str(outside),
    }).status_code == 403
    assert app.state.runtime.memory_service.resolve_project(untouched, create=False) is None


def test_patch_delete_default_reason_explicit_reason_and_invalid_combinations(
    tmp_path, monkeypatch,
):
    app, _workspace = make_app(tmp_path, monkeypatch)
    default = create_memory(app, "喜欢简洁回答")
    corrected = call(app, "PATCH", f"/memories/{default['memory_id']}", json={
        "content": "喜欢简洁中文回答", "expected_version": 1,
    })
    assert corrected.status_code == 200
    record = call(app, "GET", "/memories/feedback").json()["feedback"][0]
    assert record["reason"] == "unspecified" and record["action"] == "correct"
    assert record["origin"] == "user_api"

    explicit = create_memory(app, "以后提供更多背景")
    response = call(app, "PATCH", f"/memories/{explicit['memory_id']}", json={
        "content": "只在复杂问题提供更多背景", "expected_version": 1,
        "feedback_reason": "wording",
    })
    assert response.status_code == 200
    rows = call(app, "GET", "/memories/feedback?limit=10").json()["feedback"]
    assert any(row["old_memory_id"] == explicit["memory_id"] and row["reason"] == "wording" for row in rows)

    discard = create_memory(app, "不再保留的偏好")
    response = call(app, "DELETE", f"/memories/{discard['memory_id']}?expected_version=1")
    assert response.status_code == 200, response.text
    discard_row = next(row for row in call(app, "GET", "/memories/feedback?limit=20").json()["feedback"]
                       if row["old_memory_id"] == discard["memory_id"])
    assert discard_row["action"] == "discard" and discard_row["reason"] == "unspecified"

    for method, url, payload in (
        ("PATCH", f"/memories/{default['memory_id']}", {
            "content": "非法枚举", "expected_version": 2, "feedback_reason": "guessing",
        }),
        ("DELETE", f"/memories/{explicit['memory_id']}?expected_version=2&feedback_reason=guessing", None),
        ("DELETE", f"/memories/{explicit['memory_id']}?expected_version=2&feedback_reason=wording", None),
        ("PATCH", f"/memories/{explicit['memory_id']}", {
            "content": "拒存不能纠正", "expected_version": 2, "feedback_reason": "do_not_store",
        }),
    ):
        result = call(app, method, url, json=payload) if payload is not None else call(app, method, url)
        assert result.status_code == 422, result.text


def test_project_memory_mutations_remain_scope_authorized(tmp_path, monkeypatch):
    app, workspace = make_app(tmp_path, monkeypatch)
    project_a, project_b = workspace / "project-a", workspace / "project-b"
    project_a.mkdir()
    project_b.mkdir()
    item = create_memory(app, "项目A偏好", scope="project", workspace_path=str(project_a))
    before = call(app, "GET", "/memories/feedback", params={
        "scope": "project", "workspace_path": str(project_a),
    }).json()["feedback"]

    corrected = call(app, "PATCH", f"/memories/{item['memory_id']}", params={
        "workspace_path": str(project_b),
    }, json={"content": "越权修正", "expected_version": item["version"],
             "feedback_reason": "factual_error"})
    assert corrected.status_code == 404
    retracted = call(app, "DELETE", f"/memories/{item['memory_id']}", params={
        "workspace_path": str(project_b), "expected_version": item["version"],
        "feedback_reason": "do_not_store",
    })
    assert retracted.status_code == 404
    assert call(app, "GET", "/memories/feedback", params={
        "scope": "project", "workspace_path": str(project_a),
    }).json()["feedback"] == before
    assert call(app, "GET", f"/memories/{item['memory_id']}", params={
        "workspace_path": str(project_a),
    }).json()["status"] == "active"


def test_cas_conflict_does_not_create_feedback(tmp_path, monkeypatch):
    app, _workspace = make_app(tmp_path, monkeypatch)
    item = create_memory(app, "初始偏好")
    stale = call(app, "PATCH", f"/memories/{item['memory_id']}", json={
        "content": "一次成功修正", "expected_version": 1, "feedback_reason": "wording",
    })
    assert stale.status_code == 200
    before = call(app, "GET", "/memories/feedback?limit=10").json()["feedback"]
    conflict = call(app, "PATCH", f"/memories/{item['memory_id']}", json={
        "content": "过期版本修正", "expected_version": 1, "feedback_reason": "factual_error",
    })
    assert conflict.status_code == 409
    after = call(app, "GET", "/memories/feedback?limit=10").json()["feedback"]
    assert after == before

    stale_delete = call(app, "DELETE", f"/memories/{item['memory_id']}?expected_version=1&feedback_reason=duplicate")
    assert stale_delete.status_code == 409
    assert call(app, "GET", "/memories/feedback?limit=10").json()["feedback"] == before


def test_delete_accepts_reason_body_without_new_native_bridge_query_keys(tmp_path, monkeypatch):
    app, _workspace = make_app(tmp_path, monkeypatch)
    item = create_memory(app, "已过时的表达偏好")
    response = call(app, "DELETE", f"/memories/{item['memory_id']}?expected_version=1",
                    json={"feedback_reason": "outdated"})
    assert response.status_code == 200
    row = call(app, "GET", "/memories/feedback").json()["feedback"][0]
    assert row["action"] == "discard" and row["reason"] == "outdated"
    other = create_memory(app, "另一条表达偏好")
    for url, body in (
        (f"/memories/{other['memory_id']}?expected_version=1", {"feedback_reason": "guessing"}),
        (f"/memories/{other['memory_id']}?expected_version=1", {"feedback_reason": "wording"}),
        (f"/memories/{other['memory_id']}?expected_version=1&feedback_reason=outdated",
         {"feedback_reason": "duplicate"}),
    ):
        assert call(app, "DELETE", url, json=body).status_code == 422
    assert call(app, "GET", f"/memories/{other['memory_id']}").json()["status"] == "active"
    assert len(call(app, "GET", "/memories/feedback").json()["feedback"]) == 1
