"""Explicitly provisioned Agent Python; virtual environments are not OS sandboxes."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

PYTHON_VERSION = "3.12"
DEFAULT_REQUIREMENTS = (
    "numpy>=2.2,<3", "pandas>=2.2,<3", "matplotlib>=3.10,<4",
    "openpyxl>=3.1,<4", "pillow>=11,<13",
)
MANIFEST_NAME = "environment.json"
PREPARATION_NAME = "preparation.json"
# Allow names, not a blacklist: provider, mail, proxy and import credentials
# must not silently leak into a managed execution process.
SAFE_ENV_NAMES = {
    "PATH", "HOME", "USER", "LOGNAME", "TMPDIR", "TMP", "TEMP", "LANG",
    "LC_ALL", "LC_CTYPE", "TZ", "SYSTEMROOT", "WINDIR", "COMSPEC",
    "PATHEXT", "USERPROFILE", "LOCALAPPDATA", "APPDATA", "PROGRAMDATA",
    "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE",
}


class ManagedPythonEnvironment:
    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve(strict=False)
        self.venv = self.root / ".venv"
        # Never use the backend environment or a path inside it as the managed root.
        backend_venv = Path(sys.prefix).resolve(strict=False)
        repo_venv = Path(__file__).resolve().parents[2] / ".venv"
        if self.root.name == ".venv" or any(
            self.root == path or self.root.is_relative_to(path)
            or self.venv.resolve(strict=False) == path
            for path in {backend_venv, repo_venv}
        ):
            raise ValueError("Agent Python must use a dedicated directory outside the backend environment")

    @property
    def interpreter(self) -> Path:
        return self.venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")

    def manifest(self) -> dict[str, Any] | None:
        path = self.root / MANIFEST_NAME
        if not path.is_file():
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("schema_version") != 1:
            raise ValueError("Invalid Agent Python environment manifest")
        if value.get("environment_root") != str(self.root):
            raise ValueError("Agent Python manifest belongs to a different directory")
        if value.get("python_version_request") != PYTHON_VERSION:
            raise ValueError("Agent Python manifest has an unsupported Python version")
        lock = self.root / "requirements.lock"
        if not lock.is_file() or value.get("lock_sha256") != hashlib.sha256(lock.read_bytes()).hexdigest():
            raise ValueError("Agent Python dependency lock does not match its manifest")
        return value

    def require_available(self) -> dict[str, Any]:
        if (self.root / PREPARATION_NAME).exists():
            raise RuntimeError("Managed Python preparation is incomplete; run prepare explicitly to retry")
        manifest = self.manifest()
        if manifest is None or not self.interpreter.is_file() or not (self.venv / "pyvenv.cfg").is_file():
            raise RuntimeError("Managed Python is not prepared; run scripts/manage_agent_python.py prepare explicitly")
        # A normal POSIX venv interpreter is a symlink to the base interpreter.
        # The venv directory itself must remain the dedicated managed directory.
        if self.venv.is_symlink() or self.venv.resolve() != self.venv:
            raise ValueError("Managed Python environment directory must not be a symlink")
        return manifest

    def environment(self, workspace: Path) -> dict[str, str]:
        try:
            self.inspect()
        except subprocess.SubprocessError as exc:
            raise RuntimeError("Managed Python inspection failed") from exc
        return self._process_environment(workspace)

    def _process_environment(self, workspace: Path) -> dict[str, str]:
        self.require_available()
        env = {key: value for key, value in os.environ.items() if key.upper() in SAFE_ENV_NAMES}
        env["PATH"] = str(self.interpreter.parent) + os.pathsep + env.get("PATH", os.defpath)
        env.update({
            "VIRTUAL_ENV": str(self.venv), "LKA_PYTHON": str(self.interpreter),
            "PYTHONNOUSERSITE": "1", "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1",
            "LKA_PYTHON_SCRIPTS": str(workspace), "LKA_PYTHON_OUTPUTS": str(workspace),
        })
        return env

    def inspect(self) -> dict[str, Any]:
        """Check the running interpreter and installed lock, not just marker files."""
        manifest = self.require_available()
        completed = subprocess.run(
            [str(self.interpreter), "-I", "-c", (
                "import importlib.metadata,json,platform,sys; "
                "print(json.dumps({'python_version':platform.python_version(), "
                "'prefix':sys.prefix, 'base_prefix':sys.base_prefix, 'packages': "
                "{d.metadata['Name']:d.version for d in importlib.metadata.distributions()}}))"
            )], capture_output=True, text=True, timeout=10, check=True,
            env=self._process_environment(self.root),
        )
        installed = json.loads(completed.stdout)
        if not isinstance(installed, dict) or not isinstance(installed.get("packages"), dict):
            raise RuntimeError("Invalid managed Python inspection result")  # noqa: TRY004
        if Path(installed.get("prefix", "")).resolve() != self.venv or installed.get("prefix") == installed.get("base_prefix"):
            raise ValueError("Managed interpreter is not running in its dedicated virtual environment")
        config = (self.venv / "pyvenv.cfg").read_text(encoding="utf-8")
        if re.search(r"(?im)^include-system-site-packages\s*=\s*true\s*$", config):
            raise ValueError("Managed Python must not inherit system site packages")
        if installed.get("python_version") != manifest.get("python_version"):
            raise ValueError("Installed Python version differs from its registered manifest")
        def normalize(name: str) -> str:
            return re.sub(r"[-_.]+", "-", name).lower()
        packages = {normalize(name): version for name, version in installed["packages"].items()}
        lock = (self.root / "requirements.lock").read_text(encoding="utf-8")
        for name, version in re.findall(r"(?m)^([A-Za-z0-9_.-]+)==([^\s;\\]+)", lock):
            if packages.get(normalize(name)) != version:
                raise ValueError(f"Installed dependency differs from the managed lock: {name}=={version}")
        return {"python_version": installed["python_version"], "packages": installed["packages"]}

    def status(self, workspace: Path) -> dict[str, Any]:
        result: dict[str, Any] = {
            "name": "default", "available": False, "environment_root": str(self.root),
            "interpreter": str(self.interpreter), "python_version": None, "packages": {},
            "scripts_directory": str(workspace), "output_directory": str(workspace),
            "is_os_sandbox": False,
            "guidance": "Write a .py file with filesystem.create_file; run it with bash.run "
            "python_environment=default and the LKA_PYTHON interpreter; inspect exit code, output and artifacts; "
            "correct the script using filesystem.edit_file. Dependencies are provisioned explicitly by the operator.",
        }
        try:
            result.update(self.inspect())
            result["available"] = True
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
            result["error"] = str(exc)
        return result
