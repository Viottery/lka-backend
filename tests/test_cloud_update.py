import json
import sqlite3
from pathlib import Path

import pytest

from scripts.deploy import update_cloud


def make_config(tmp_path):
    paths = {
        "backend": tmp_path / "opt/lka-backend",
        "frontend": tmp_path / "opt/lka-desktop",
        "data": tmp_path / "var/lib/lka",
        "env_file": tmp_path / "etc/lka/backend.env",
        "config_file": tmp_path / "etc/lka/local.toml",
        "service": "lka-cloud.service",
        "health_url": "http://127.0.0.1:8765",
        "releases": tmp_path / "opt/lka-releases",
        "state": tmp_path / "var/lib/lka-update",
        "uv": tmp_path / "bin/uv",
        "snapshot_script": tmp_path / "opt/lka-backend/scripts/data_snapshot.py",
        "guard_files": [],
    }
    for key in ("backend", "frontend", "data", "releases", "state"):
        paths[key].mkdir(parents=True, exist_ok=True)
    paths["env_file"].parent.mkdir(parents=True, exist_ok=True)
    paths["env_file"].write_text("LKA_DATA_DIR=/var/lib/lka\n")
    paths["config_file"].write_text("[auth]\nenabled=true\n")
    paths["snapshot_script"].parent.mkdir(parents=True, exist_ok=True)
    paths["snapshot_script"].write_text("# snapshot helper\n")
    paths["uv"].parent.mkdir(parents=True, exist_ok=True)
    paths["uv"].write_text("")
    c = {key: str(value) if isinstance(value, Path) else value for key, value in paths.items()}
    c["guard_files"] = []
    return c


def make_db(data_dir, *, run_status=None):
    path = Path(data_dir) / "lka.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE agent_runs(status TEXT)")
        conn.execute("CREATE TABLE background_jobs(status TEXT)")
        conn.execute("CREATE TABLE remote_execution_tasks(state TEXT)")
        if run_status:
            conn.execute("INSERT INTO agent_runs VALUES (?)", (run_status,))
    return path


def candidate_plan(c, *, previous=None, target=None):
    candidate = Path(c["releases"]) / "candidate-1"
    for component in ("backend", "frontend"):
        root = candidate / component
        (root / ".venv/bin").mkdir(parents=True, exist_ok=True)
        if component == "backend":
            (root / ".venv/bin/python").write_text("python")
    previous = previous or {
        "backend": {"path": c["backend"], "head": "a" * 40},
        "frontend": {"path": c["frontend"], "head": "b" * 40},
    }
    target = target or {"version": "release-2", "backend": "c" * 40, "frontend": "d" * 40}
    plan = {"previous": previous, "target": target, "guard": update_cloud.guard(c),
            "stage": "prepared", "candidate": str(candidate)}
    update_cloud.save(Path(c["state"]) / "prepared.json", plan)
    return plan


def test_checkout_rejects_dirty_tree_and_wrong_expected_head(monkeypatch, tmp_path):
    root = tmp_path / "checkout"
    root.mkdir()
    responses = iter([str(root), update_cloud.REPOS["backend"], " M app/core/runtime.py"])
    monkeypatch.setattr(update_cloud, "git", lambda *_args, **_kwargs: next(responses))
    with pytest.raises(update_cloud.UpdateError, match="local changes"):
        update_cloud.checkout(root, "backend")

    responses = iter([str(root), update_cloud.REPOS["backend"], "", "e" * 40])
    monkeypatch.setattr(update_cloud, "git", lambda *_args, **_kwargs: next(responses))
    with pytest.raises(update_cloud.UpdateError, match="changed since preparation"):
        update_cloud.checkout(root, "backend", "f" * 40)


@pytest.mark.parametrize("ref", ["../main", "main..other", "-c", "x" * 152, "feature\\bad"])
def test_release_ref_rejects_unsafe_values_before_network_or_git(monkeypatch, tmp_path, ref):
    c = make_config(tmp_path)
    monkeypatch.setattr(update_cloud, "git", lambda *_a, **_kw: pytest.fail("git must not run"))
    with pytest.raises(update_cloud.UpdateError, match="Invalid frontend release ref"):
        update_cloud.targets(c, ref)


@pytest.mark.parametrize("manifest", [
    {"format": 2, "version": "v1", "backend_commit": "a" * 40},
    {"format": 1, "version": "../v1", "backend_commit": "a" * 40},
    {"format": 1, "version": "v1", "backend_commit": "a" * 39},
])
def test_release_manifest_rejects_invalid_pair_identity(manifest):
    with pytest.raises(update_cloud.UpdateError, match="Invalid paired release manifest"):
        update_cloud.release_manifest(json.dumps(manifest))


@pytest.mark.parametrize("raw", ["null", "[]", "\"release\""])
def test_release_manifest_rejects_non_object_json(raw):
    with pytest.raises(update_cloud.UpdateError, match="Invalid paired release manifest"):
        update_cloud.release_manifest(raw)


def test_prepare_dependency_failure_never_stops_service(monkeypatch, tmp_path):
    c = make_config(tmp_path)
    (Path(c["backend"]) / ".venv/bin").mkdir(parents=True)
    (Path(c["backend"]) / ".venv/bin/python").write_text("python")
    make_db(c["data"])
    active = {"backend": {"path": c["backend"], "head": "a" * 40},
              "frontend": {"path": c["frontend"], "head": "b" * 40}}
    monkeypatch.setattr(update_cloud, "current", lambda _c: active)
    monkeypatch.setattr(update_cloud, "checkout", lambda *_a, **_kw: None)

    def fake_run(args, **_kwargs):
        if args[0] == "git" and "clone" in args:
            Path(args[-1]).mkdir(parents=True, exist_ok=True)
            return ""
        if str(args[0]).endswith("/uv"):
            raise update_cloud.UpdateError("dependency preparation failed")
        return ""

    monkeypatch.setattr(update_cloud, "run", fake_run)
    service_calls = []
    monkeypatch.setattr(update_cloud, "service", lambda _c, action: service_calls.append(action))
    target = {"version": "release-2", "backend": "c" * 40, "frontend": "d" * 40}
    with pytest.raises(update_cloud.UpdateError, match="dependency preparation failed"):
        update_cloud.prepare(c, target)
    assert service_calls == []


def test_apply_refuses_active_tasks_before_stopping_service(monkeypatch, tmp_path):
    c = make_config(tmp_path)
    make_db(c["data"], run_status="waiting_confirmation")
    candidate_plan(c)
    service_calls = []
    monkeypatch.setattr(update_cloud, "current", lambda _c: json.loads(
        (Path(c["state"]) / "prepared.json").read_text())["previous"])
    monkeypatch.setattr(update_cloud, "checkout", lambda *_a, **_kw: None)
    monkeypatch.setattr(update_cloud, "service", lambda _c, action: service_calls.append(action))
    monkeypatch.setattr(update_cloud.shutil, "disk_usage", lambda _p: type("Usage", (), {"free": 10**12})())
    with pytest.raises(update_cloud.UpdateError, match="Tasks are active"):
        update_cloud.apply(c)
    assert service_calls == []
    assert not (Path(c["state"]) / "transaction.json").exists()


def test_apply_creates_verified_backup_before_switch(monkeypatch, tmp_path):
    c = make_config(tmp_path)
    make_db(c["data"])
    plan = candidate_plan(c)
    monkeypatch.setattr(update_cloud, "current", lambda _c: plan["previous"])
    monkeypatch.setattr(update_cloud, "checkout", lambda *_a, **_kw: None)
    monkeypatch.setattr(update_cloud, "require_idle", lambda _c: None)
    monkeypatch.setattr(update_cloud.shutil, "disk_usage", lambda _p: type("Usage", (), {"free": 10**12})())
    events = []
    monkeypatch.setattr(update_cloud, "service", lambda _c, action: events.append(("service", action)))
    monkeypatch.setattr(update_cloud, "retain", lambda _c, _previous: events.append(("retain", None)))
    monkeypatch.setattr(update_cloud, "point", lambda link, target: events.append(("point", str(link), str(target))))
    monkeypatch.setattr(update_cloud, "wait_health", lambda *_a: events.append(("health", None)))

    def fake_run(args, **_kwargs):
        if "create" in args and "--offline" in args:
            output = Path(args[args.index("--output") + 1])
            output.mkdir()
            (output / "manifest.json").write_text('{"verified": true}')
            events.append(("backup", output))
            return ""
        pytest.fail(f"unexpected external command: {args}")

    monkeypatch.setattr(update_cloud, "run", fake_run)
    result = update_cloud.apply(c)
    names = [event[0] for event in events]
    assert result["applied"] is True
    assert names.index("backup") < names.index("retain") < names.index("point")
    assert (Path(c["state"]) / "last-applied.json").is_file()
    assert not (Path(c["state"]) / "transaction.json").exists()


def test_health_failure_rolls_back_code_only_and_keeps_live_database(monkeypatch, tmp_path):
    c = make_config(tmp_path)
    db = make_db(c["data"])
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE user_data(value TEXT)")
        conn.execute("INSERT INTO user_data VALUES ('keep-me')")

    old_roots = {}
    for component in ("backend", "frontend"):
        old = Path(c["releases"]) / f"old-{component}"
        old.mkdir()
        link = Path(c[component])
        import shutil
        shutil.rmtree(link)
        link.symlink_to(old, target_is_directory=True)
        old_roots[component] = str(old)
    previous = {component: {"path": old_roots[component], "head": "a" * 40}
                for component in ("backend", "frontend")}
    plan = candidate_plan(c, previous=previous)
    monkeypatch.setattr(update_cloud, "current", lambda _c: plan["previous"])
    monkeypatch.setattr(update_cloud, "checkout", lambda *_a, **_kw: None)
    monkeypatch.setattr(update_cloud, "require_idle", lambda _c: None)
    monkeypatch.setattr(update_cloud.shutil, "disk_usage", lambda _p: type("Usage", (), {"free": 10**12})())
    events = []
    monkeypatch.setattr(update_cloud, "service", lambda _c, action: events.append(action))
    monkeypatch.setattr(update_cloud, "retain", lambda *_a: None)

    def fake_run(args, **_kwargs):
        if "create" in args and "--offline" in args:
            output = Path(args[args.index("--output") + 1])
            output.mkdir()
            (output / "manifest.json").write_text('{"verified": true}')
            return ""
        pytest.fail(f"unexpected external command: {args}")

    monkeypatch.setattr(update_cloud, "run", fake_run)
    health_calls = 0

    def fail_first_health(*_args):
        nonlocal health_calls
        health_calls += 1
        if health_calls == 1:
            raise update_cloud.UpdateError("candidate health failed")

    monkeypatch.setattr(update_cloud, "wait_health", fail_first_health)
    with pytest.raises(update_cloud.UpdateError, match="candidate health failed"):
        update_cloud.apply(c)
    assert all(Path(c[component]).resolve() == Path(old_roots[component]).resolve()
               for component in ("backend", "frontend"))
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT value FROM user_data").fetchone()[0] == "keep-me"
    assert events == ["stop", "start", "stop", "start"]
    assert not (Path(c["state"]) / "transaction.json").exists()


def test_interrupted_transaction_blocks_next_apply_without_touching_service(monkeypatch, tmp_path):
    c = make_config(tmp_path)
    make_db(c["data"])
    candidate_plan(c)
    transaction = Path(c["state"]) / "transaction.json"
    transaction.write_text('{"stage":"interrupted"}')
    service_calls = []
    monkeypatch.setattr(update_cloud, "service", lambda _c, action: service_calls.append(action))
    with pytest.raises(update_cloud.UpdateError, match="interrupted transaction"):
        update_cloud.apply(c)
    assert service_calls == []
    assert transaction.is_file()
