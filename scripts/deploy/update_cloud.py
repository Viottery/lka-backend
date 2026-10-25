"""Paired Git release preparation and guarded systemd cutover (Linux, root).

Preparation never stops the service. Application data is never auto-restored.
The separately installed data_snapshot.py is the trusted backup implementation.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import time
import uuid
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

REPOS = {"backend": "https://github.com/Viottery/lka-backend.git",
         "frontend": "https://github.com/Viottery/lka-desktop.git"}
TERMINAL = {"completed", "failed", "cancelled", "timed_out"}


class UpdateError(Exception):
    pass


def save(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)


def run(args, *, cwd=None, env=None, log=None):
    result = subprocess.run([str(v) for v in args], cwd=cwd, env=env,
                            capture_output=True, timeout=1800, check=False)
    if log:
        with log.open("ab") as stream:
            stream.write(result.stdout + result.stderr)
    if result.returncode:
        raise UpdateError(f"{Path(str(args[0])).name} failed; inspect the private update log.")
    return result.stdout.decode("utf-8", errors="replace").strip()


def git(root, *args, log=None):
    # Per-command trust only; never change the machine's global safe.directory.
    return run(["git", "-c", f"safe.directory={root}", "-C", root, *args], log=log)


def config(path):
    c = json.loads(path.read_text())
    required = {"backend", "frontend", "data", "env_file", "config_file", "service",
                "health_url", "releases", "state", "uv", "snapshot_script", "guard_files"}
    if (not isinstance(c, dict) or set(c) != required
            or not isinstance(c["service"], str)
            or not re.fullmatch(r"[A-Za-z0-9_.@-]+\.service", c["service"])):
        raise UpdateError("Invalid updater configuration.")
    for key in required - {"service", "health_url", "guard_files"}:
        if not Path(c[key]).is_absolute():
            raise UpdateError("Updater paths must be absolute.")
    if not re.fullmatch(r"http://127\.0\.0\.1:[0-9]+", c["health_url"]):
        raise UpdateError("Health checks must use the local loopback backend.")
    data, releases, state = (Path(c[k]).resolve() for k in ("data", "releases", "state"))
    if any(a == b or a in b.parents or b in a.parents
           for a, b in ((data, releases), (data, state), (releases, state))):
        raise UpdateError("Data, release and updater state directories must be separate.")
    for component in REPOS:
        code = Path(c[component]).resolve()
        if any(path == code or code in path.parents
               for path in (data, state, Path(c["env_file"]).resolve(), Path(c["config_file"]).resolve())):
            raise UpdateError("Live data and configuration must be outside the code checkouts.")
    return c


@contextmanager
def lock(c):
    state = Path(c["state"])
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (state / "lock").open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise UpdateError("Another cloud update is running.") from exc
        yield


def checkout(root, component, expected=None):
    root = Path(root).resolve(strict=True)
    if Path(git(root, "rev-parse", "--show-toplevel")).resolve() != root:
        raise UpdateError("Not a checkout root.")
    if git(root, "remote", "get-url", "origin") != REPOS[component]:
        raise UpdateError(f"Unexpected {component} repository.")
    if git(root, "status", "--porcelain", "--untracked-files=all"):
        raise UpdateError(f"{component} has local changes; preserve them before updating.")
    head = git(root, "rev-parse", "HEAD")
    if expected and head != expected:
        raise UpdateError(f"{component} changed since preparation.")
    return {"path": str(root), "head": head}


def service(c, action):
    return run(["systemctl", action, c["service"]], log=Path(c["state"]) / "update.log")


def guard(c):
    paths = [c["env_file"], c["config_file"], *c["guard_files"]]
    return {name: hashlib.sha256(Path(name).read_bytes()).hexdigest() for name in paths}


def idle(c):
    db = Path(c["data"]) / "lka.sqlite3"
    with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True, timeout=5)) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        counts = {}
        for table, column, terminal in (
            ("agent_runs", "status", TERMINAL),
            ("background_jobs", "status", {"queued", "retry_wait", "succeeded", "failed", "cancelled"}),
            ("remote_execution_tasks", "state", {"completed", "failed", "cancelled", "unknown"}),
        ):
            if table in tables:
                values = dict(conn.execute(f"SELECT {column},count(*) FROM {table} GROUP BY {column}"))
                counts[table] = sum(v for k, v in values.items() if k not in terminal)
        if "agent_runs" not in tables:
            raise UpdateError("Not an initialized backend database.")
    return counts


def require_idle(c):
    if any(idle(c).values()):
        raise UpdateError("Tasks are active, paused, awaiting input or remotely leased; finish them before applying.")


def request(c, path):
    opener = build_opener(ProxyHandler({}))
    try:
        # Match the existing trusted loopback HTTPS reverse proxy. Without this
        # header the HTTPS-only gate rejects before checking anonymous identity.
        req = Request(c["health_url"] + path, headers={"X-Forwarded-Proto": "https"})
        with opener.open(req, timeout=3) as response:
            return response.status, response.read()
    except HTTPError as exc:
        return exc.code, b""


def health(c, backend):
    try:
        code, raw = request(c, "/health")
        value = json.loads(raw)
        if code != 200 or value.get("status") != "ok" or value.get("service") != "local-knowledge-agent-os":
            return False
        code, raw = request(c, "/client/info")
        if code != 200 or json.loads(raw).get("role") != "hub":
            return False
        if request(c, "/sessions?limit=1")[0] != 401:
            return False
        pid = run(["systemctl", "show", c["service"], "-p", "MainPID", "--value"])
        return pid.isdigit() and pid != "0" and Path(f"/proc/{pid}/cwd").resolve() == Path(backend).resolve()
    except (OSError, ValueError, URLError, UpdateError):
        return False


def current(c):
    result = {k: checkout(c[k], k) for k in REPOS}
    if service(c, "is-active") != "active":
        raise UpdateError("Expected cloud service is not active.")
    if not health(c, c["backend"]):
        raise UpdateError("Current cloud identity or authentication health check failed.")
    return result


def release_manifest(raw):
    if len(raw) > 16384:
        raise UpdateError("Release manifest is too large.")
    value = json.loads(raw)
    if (not isinstance(value, dict) or value.get("format") != 1 or not isinstance(value.get("version"), str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", value["version"])
            or not re.fullmatch(r"[0-9a-f]{40}", str(value.get("backend_commit", "")))):
        raise UpdateError("Invalid paired release manifest.")
    return value


def targets(c, ref):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,150}", ref) or ".." in ref:
        raise UpdateError("Invalid frontend release ref.")
    log = Path(c["state"]) / "update.log"
    cache = Path(c["state"]) / "git"
    cache.mkdir(exist_ok=True)
    for component in ("frontend", "backend"):
        repo = cache / component
        if not repo.exists():
            run(["git", "init", "--bare", repo], log=log)
            git(repo, "remote", "add", "origin", REPOS[component], log=log)
        if git(repo, "remote", "get-url", "origin") != REPOS[component]:
            raise UpdateError("Unexpected release cache remote.")
    front = cache / "frontend"
    git(front, "fetch", "--no-tags", "origin", ref, log=log)
    front_head = git(front, "rev-parse", "FETCH_HEAD^{commit}")
    manifest = release_manifest(git(front, "show", f"{front_head}:release-channel.json"))
    back = cache / "backend"
    git(back, "fetch", "--no-tags", "origin", manifest["backend_commit"], log=log)
    if git(back, "rev-parse", "FETCH_HEAD^{commit}") != manifest["backend_commit"]:
        raise UpdateError("Backend does not match release manifest.")
    return {"version": manifest["version"], "backend": manifest["backend_commit"], "frontend": front_head}


def prepare(c, target):
    before = current(c)
    plan = {"previous": before, "target": target, "guard": guard(c), "stage": "preparing"}
    name = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8]
    root = Path(c["releases"]) / name
    root.mkdir(mode=0o755, parents=True)
    log = Path(c["state"]) / "update.log"
    for component, remote in REPOS.items():
        destination = root / component
        run(["git", "clone", "--no-hardlinks", Path(c["state"]) / "git" / component, destination], log=log)
        git(destination, "remote", "set-url", "origin", remote)
        git(destination, "checkout", "--detach", target[component], log=log)
        checkout(destination, component, target[component])
    python = (Path(c["backend"]) / ".venv/bin/python").resolve(strict=True)
    run([c["uv"], "sync", "--locked", "--no-dev", "--python", python,
         "--project", root / "backend", "--cache-dir", Path(c["state"]) / "uv-cache"], log=log)
    candidate = root / "backend"
    # dotenv parses EnvironmentFile as data, never shell source. systemd Environment
    # also carries existing transport/config-library adaptations.
    from dotenv import dotenv_values
    environment = dict(os.environ)
    for entry in shlex.split(run(["systemctl", "show", c["service"], "-p", "Environment", "--value"])):
        key, _, value = entry.partition("=")
        environment[key] = value
    environment.update({k: v for k, v in dotenv_values(c["env_file"]).items() if v is not None})
    for field, name in (("LKA_DATA_DIR", "data"), ("LKA_LOCAL_CONFIG", "config_file")):
        if Path(environment.get(field, "")).resolve() != Path(c[name]).resolve():
            raise UpdateError("Service data/configuration paths differ from updater settings.")
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["LKA_UPDATE_FRONTEND_ASSET_ROOT"] = str(Path(c["frontend"]) / "app/web/pet")
    account = run(["systemctl", "show", c["service"], "-p", "User", "--value"])
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", account):
        raise UpdateError("Expected a dedicated service account.")
    run(["runuser", "-u", account, "--", candidate / ".venv/bin/python", "-c",
         ("import os; from pathlib import Path; from app.core.config import Settings; "
          "s=Settings(); c=s.load_local_config(); "
         "assert c.auth.enabled and c.auth.require_https and c.auth.secure_cookie; "
         "assert c.sync.enabled and c.sync.role=='hub'; "
         "assert Path(c.client.workbench_dir)==Path(os.environ['LKA_UPDATE_FRONTEND_ASSET_ROOT']); "
         "print('configuration compatible')")], cwd=candidate, env=environment, log=log)
    plan.update(candidate=str(root), stage="prepared")
    save(Path(c["state"]) / "prepared.json", plan)
    return plan


def point(link, target):
    """Atomic symlink replacement; the first conversion retains the old directory."""
    link, target = Path(link), Path(target)
    if link.resolve() == target.resolve():
        return
    if not link.is_symlink():
        raise UpdateError("Active directory must be retained before first symlink conversion.")
    temporary = link.with_name(link.name + ".update-" + uuid.uuid4().hex[:8])
    temporary.symlink_to(target, target_is_directory=True)
    temporary.replace(link)


def retain(c, previous):
    for component in REPOS:
        link = Path(c[component])
        if not link.is_symlink():
            retained = link.with_name(link.name + ".retained-" + uuid.uuid4().hex[:8])
            # Persist the original directory's new path before creating a link.
            previous[component]["path"] = str(retained)
            save(Path(c["state"]) / "transaction.json", previous)
            link.rename(retained)
            link.symlink_to(retained, target_is_directory=True)


def wait_health(c, backend):
    for _ in range(60):
        if health(c, backend):
            return
        time.sleep(1)
    raise UpdateError("Release health check failed; consult the service journal.")


def apply(c):
    state = Path(c["state"])
    transaction = state / "transaction.json"
    if transaction.exists():
        raise UpdateError("An interrupted transaction exists; inspect it before another apply.")
    plan = json.loads((state / "prepared.json").read_text())
    if plan.get("stage") != "prepared" or guard(c) != plan["guard"] or current(c) != plan["previous"]:
        raise UpdateError("Deployment/configuration changed since preparation; prepare again.")
    candidate = Path(plan["candidate"])
    if candidate.parent.resolve() != Path(c["releases"]).resolve():
        raise UpdateError("Candidate must be a direct child of the release directory.")
    for component in REPOS:
        checkout(candidate / component, component, plan["target"][component])
    if all(plan["previous"][k]["head"] == plan["target"][k] for k in REPOS):
        return {"applied": False, "reason": "already_current"}
    if not (candidate / "backend/.venv/bin/python").is_file():
        raise UpdateError("Prepared backend environment is missing.")
    require_idle(c)
    size = sum(p.stat().st_size for p in Path(c["data"]).rglob("*") if p.is_file() and not p.is_symlink())
    if shutil.disk_usage(state).free < size + 1024**3:
        raise UpdateError("Not enough free space for a verified data snapshot.")
    previous = plan["previous"]
    save(transaction, previous)
    switched = False
    try:
        service(c, "stop")
        # Refuse a task accepted between the initial idle check and systemd stop.
        require_idle(c)
        backup = state / ("snapshot-" + candidate.name)
        run([Path(c["backend"]) / ".venv/bin/python", c["snapshot_script"], "create", "--offline",
             "--data-dir", c["data"], "--env-file", c["env_file"], "--config-file", c["config_file"],
             "--output", backup], log=state / "update.log")
        if guard(c) != plan["guard"]:
            raise UpdateError("Configuration changed while the snapshot was created.")
        retain(c, previous)
        switched = True
        for component in REPOS:
            point(c[component], candidate / component)
        service(c, "start")
        wait_health(c, candidate / "backend")
        plan.update(stage="applied", backup=str(backup), previous=previous)
        save(state / "prepared.json", plan)
        save(state / "last-applied.json", plan)
        transaction.unlink()
        return {"applied": True, "version": plan["target"]["version"], "backup": str(backup)}
    except BaseException:
        # Code and venv only: a newly started version may already have written data.
        service(c, "stop")
        for component in REPOS:
            link, old = Path(c[component]), Path(previous[component]["path"])
            if link.is_symlink():
                point(link, old)
            elif not link.exists() and old.exists():
                link.symlink_to(old, target_is_directory=True)
        service(c, "start")
        wait_health(c, c["backend"])
        plan["stage"] = "failed_after_switch" if switched else "failed_before_switch"
        save(state / "prepared.json", plan)
        transaction.unlink()
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("status", "check", "prepare", "apply", "update"))
    parser.add_argument("--config", type=Path, default=Path("/etc/lka/update.json"))
    parser.add_argument("--ref", default="main", help="frontend branch, release tag or commit; its manifest pins backend")
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise UpdateError("Run this host maintenance command with sudo.")
    os.umask(0o022)
    c = config(args.config)
    with lock(c):
        if args.action == "apply":
            result = apply(c)
        else:
            before = current(c)
            result = {"current": before, "active_tasks": idle(c)}
            if args.action in {"check", "prepare", "update"}:
                target = targets(c, args.ref)
                result.update(target=target, update_available=any(before[k]["head"] != target[k] for k in REPOS))
                if args.action == "prepare" or args.action == "update" and result["update_available"]:
                    result["prepared"] = prepare(c, target)["candidate"]
                if args.action == "update" and result["update_available"]:
                    result["application"] = apply(c)
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (UpdateError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        # Never print provider values, subprocess output or private configuration.
        message = str(exc) if isinstance(exc, UpdateError) else type(exc).__name__
        raise SystemExit("Cloud update stopped: " + message) from None
