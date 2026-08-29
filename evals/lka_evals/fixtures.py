"""Fixture loading and isolated test-data injection."""

from __future__ import annotations

import json
import shutil
from hashlib import sha1
from pathlib import Path
from typing import Any

from app.domains.mail import MailAccountInput, MailMessageInput
from app.domains.matters import MatterCreateInput


FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures"


def load_fixture(relative_path: str) -> dict[str, Any]:
    path = FIXTURE_ROOT / relative_path
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Fixture root must be an object: {path}")
    return payload


def apply_setup(runtime: Any, setup: dict[str, Any] | None) -> dict[str, Any]:
    """Apply deterministic setup to an isolated runtime and return fixture index."""

    setup = setup or {}
    fixture_index: dict[str, Any] = {
        "mail": {"messages_by_external_id": {}, "account_ids": []},
        "matters": {"matter_ids": []},
        "filesystem": {},
        "workspaces": [],
    }
    mail_fixtures = setup.get("mail_fixtures")
    if isinstance(mail_fixtures, list):
        for fixture_name in mail_fixtures:
            if isinstance(fixture_name, str):
                _import_mail_fixture(runtime, fixture_name, fixture_index)

    inline_mail = setup.get("mail")
    if isinstance(inline_mail, dict):
        _import_mail_payload(runtime, inline_mail, fixture_index)

    matters = setup.get("matters")
    if isinstance(matters, list):
        for matter_payload in matters:
            if not isinstance(matter_payload, dict):
                continue
            matter = runtime.create_matter(
                payload=MatterCreateInput.model_validate(matter_payload)
            )
            fixture_index["matters"]["matter_ids"].append(matter.matter_id)

    workspaces = setup.get("workspaces")
    if isinstance(workspaces, list):
        for workspace_payload in workspaces:
            if not isinstance(workspace_payload, dict):
                continue
            workspace = workspace_payload.get("workspace")
            if not isinstance(workspace, str):
                continue
            response = runtime.index_workspace(
                workspace=workspace,
                source_frontend=workspace_payload.get("source_frontend"),
                options=workspace_payload.get("options")
                if isinstance(workspace_payload.get("options"), dict)
                else None,
            )
            fixture_index["workspaces"].append(response.model_dump(mode="json"))

    return fixture_index


def prepare_filesystem_fixture(
    *,
    temp_path: Path,
    setup: dict[str, Any] | None,
) -> dict[str, Any]:
    setup = setup or {}
    fixture_name = setup.get("file_workspace_fixture")
    if not isinstance(fixture_name, str) or not fixture_name:
        return {}

    source = FIXTURE_ROOT / "files" / fixture_name
    if not source.exists() or not source.is_dir():
        raise ValueError(f"File workspace fixture not found: {source}")
    target = temp_path / "file_workspaces" / fixture_name
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target)
    return _index_filesystem_fixture(target)


def snapshot_filesystem_fixture(fixture_index: dict[str, Any]) -> dict[str, Any]:
    filesystem = fixture_index.get("filesystem")
    if not isinstance(filesystem, dict):
        return {}
    root_value = filesystem.get("workspace_root")
    if not isinstance(root_value, str) or not root_value:
        return {}
    root = Path(root_value)
    snapshot: dict[str, Any] = {"files": {}}
    if not root.exists() or not root.is_dir():
        return snapshot
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative_path = path.relative_to(root).as_posix()
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeError:
            content = ""
        snapshot["files"][relative_path] = {
            "exists": True,
            "sha256": _sha256(path),
            "content": content,
        }
    return snapshot


def _import_mail_fixture(runtime: Any, fixture_name: str, fixture_index: dict[str, Any]) -> None:
    relative_path = fixture_name
    if not relative_path.endswith(".json"):
        relative_path = f"mail/{relative_path}.json"
    _import_mail_payload(runtime, load_fixture(relative_path), fixture_index)


def _import_mail_payload(runtime: Any, payload: dict[str, Any], fixture_index: dict[str, Any]) -> None:
    account_payload = payload.get("account")
    messages_payload = payload.get("messages")
    if not isinstance(account_payload, dict) or not isinstance(messages_payload, list):
        raise ValueError("Mail fixture requires account object and messages list.")
    account = MailAccountInput.model_validate(account_payload)
    messages = [
        MailMessageInput.model_validate(message)
        for message in messages_payload
        if isinstance(message, dict)
    ]
    result = runtime.import_mail(account=account, messages=messages)
    fixture_index["mail"]["account_ids"].append(result.account_id)
    for message in messages:
        fixture_index["mail"]["messages_by_external_id"][message.external_id] = {
            "message_id": _stable_id("mail_msg", result.account_id, message.external_id),
            "subject": message.subject,
            "received_at": message.received_at,
        }


def _stable_id(prefix: str, *parts: str | None) -> str:
    text = "|".join(part or "" for part in parts)
    digest = sha1(text.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}_{digest}"


def _index_filesystem_fixture(root: Path) -> dict[str, Any]:
    files: dict[str, Any] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative_path = path.relative_to(root).as_posix()
        files[relative_path] = {
            "path": path.as_posix(),
            "sha256": _sha256(path),
            "size_bytes": path.stat().st_size,
        }
    return {"workspace_root": root.as_posix(), "files": files}


def _sha256(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()
