"""Prepare a separate uv-managed Python environment; never runs during Agent turns."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from app.platform.python_environment import (
    DEFAULT_REQUIREMENTS,
    MANIFEST_NAME,
    PREPARATION_NAME,
    PYTHON_VERSION,
    SAFE_ENV_NAMES,
    ManagedPythonEnvironment,
)


@contextmanager
def preparation_lock(root: Path):
    """Hold an OS lock; process failure releases it without a stale-lock retry."""
    root.parent.mkdir(parents=True, exist_ok=True)
    path = root.parent / f".{root.name}.prepare.lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "r+b") as stream:
        try:
            if os.name == "nt":
                import msvcrt
                if not path.stat().st_size:
                    stream.write(b"\0")
                    stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("Agent Python preparation is already running") from exc
        try:
            yield
        finally:
            if os.name == "nt":
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        # Keep the inode: unlinking permits two callers to lock different files.


def prepare(environment: ManagedPythonEnvironment) -> dict:
    with preparation_lock(environment.root):
        return _prepare_locked(environment)


def _prepare_locked(environment: ManagedPythonEnvironment) -> dict:
    uv = shutil.which("uv")
    if not uv:
        raise RuntimeError("Install uv before preparing Agent Python")
    root = environment.root
    marker = root / PREPARATION_NAME
    owned = False
    if marker.exists():
        owned = json.loads(marker.read_text(encoding="utf-8")) == {"environment_root": str(root), "schema_version": 1}
        if not owned:
            raise ValueError("Preparation marker belongs to another environment")
    if (root / MANIFEST_NAME).is_file():
        # A valid registered lock authorizes repair of missing/broken installs.
        environment.manifest()
        owned = True
    if root.exists() and any(root.iterdir()) and not owned:
        raise ValueError("Refusing to provision into a nonempty unrelated directory")
    if environment.venv.exists() and not owned:
        raise ValueError("Refusing to reuse an unregistered virtual environment")
    root.mkdir(parents=True, exist_ok=True)
    if not marker.exists():
        with marker.open("x", encoding="utf-8") as stream:
            json.dump({"environment_root": str(root), "schema_version": 1}, stream)
    requirements = root / "requirements.in"
    expected = "\n".join(DEFAULT_REQUIREMENTS) + "\n"
    if requirements.exists() and requirements.read_text(encoding="utf-8") != expected:
        raise ValueError("Existing requirements differ from the managed default; preserve and review manually")
    if not requirements.exists():
        requirements.write_text(expected, encoding="utf-8")
    # Credentials, private indexes, project settings and service secrets do not
    # participate in environment installation or child interpreter inspection.
    env = {key: value for key, value in os.environ.items() if key.upper() in SAFE_ENV_NAMES}
    env["UV_CACHE_DIR"] = str(root / "uv-cache")
    def run(*arguments: str) -> None:
        subprocess.run([uv, "--no-config", *arguments], cwd=root, env=env, check=True)
    lock = root / "requirements.lock"
    if not lock.exists():
        pending_lock = root / "requirements.pending.lock"
        try:
            run("pip", "compile", str(requirements), "--python-version", PYTHON_VERSION,
                "--generate-hashes", "--output-file", str(pending_lock))
            pending_lock.replace(lock)
        finally:
            pending_lock.unlink(missing_ok=True)
    if environment.venv.is_symlink():
        raise ValueError("Managed environment must not be a symlink")
    if not environment.interpreter.is_file() or not (environment.venv / "pyvenv.cfg").is_file():
        run("venv", "--python", PYTHON_VERSION, "--allow-existing", str(environment.venv))
    run("pip", "sync", "--python", str(environment.interpreter), "--require-hashes", str(lock))
    version = subprocess.run(
        [str(environment.interpreter), "-I", "-c", "import platform; print(platform.python_version())"],
        env=env, check=True, capture_output=True, text=True,
    ).stdout.strip()
    if not version.startswith(PYTHON_VERSION + "."):
        raise ValueError("Managed Python version does not match the configured version")
    manifest = {"schema_version": 1, "environment_root": str(root), "python_version": version,
                "python_version_request": PYTHON_VERSION, "requirements": list(DEFAULT_REQUIREMENTS),
                "lock_file": "requirements.lock", "lock_sha256": hashlib.sha256(lock.read_bytes()).hexdigest()}
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=root, delete=False) as stream:
        pending_manifest = Path(stream.name)
        stream.write(json.dumps(manifest, indent=2) + "\n")
    try:
        pending_manifest.replace(root / MANIFEST_NAME)
    finally:
        pending_manifest.unlink(missing_ok=True)
    marker.unlink()
    return environment.status(Path.cwd())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "status"))
    parser.add_argument("--root", type=Path, default=None,
                        help="Dedicated Agent Python directory; defaults to configured data_dir/python")
    args = parser.parse_args(argv)
    if args.root is None:
        from app.core.config import get_settings
        args.root = get_settings().data_dir / "python"
    environment = ManagedPythonEnvironment(args.root)
    result = prepare(environment) if args.action == "prepare" else environment.status(Path.cwd())
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result["available"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
