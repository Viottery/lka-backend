"""Frontend-safe memory/background configuration, applied on restart."""

from __future__ import annotations

import math
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.api.routes.memories import require_local_memory_control
from app.core.local_config import BackgroundConfig, MemoryConfig
from app.domains.memory_settings import SettingsConflictError

router = APIRouter(tags=["memory"], dependencies=[Depends(require_local_memory_control)])


class SettingsPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=0)
    memory: dict[str, Any] = Field(default_factory=dict)
    background: dict[str, Any] = Field(default_factory=dict)


def _merged(runtime, overrides):
    return {
        section: {**getattr(runtime.memory_settings_base, section).model_dump(mode="json"),
                  **(overrides.get(section, {}) if isinstance(overrides.get(section, {}), dict) else {})}
        for section in ("memory", "background")
    }


def _validate(runtime, desired):
    if any(isinstance(value, float) and not math.isfinite(value)
           for section in desired.values() for value in section.values()):
        raise HTTPException(422, detail="configuration numbers must be finite")
    try:
        memory = MemoryConfig.model_validate(desired["memory"], strict=True)
        BackgroundConfig.model_validate(desired["background"], strict=True)
    except ValidationError as exc:
        # Return field errors without echoing submitted values or config objects.
        raise HTTPException(422, detail=[{"loc": list(e["loc"]), "type": e["type"],
                                        "msg": e["msg"]} for e in exc.errors()]) from exc
    clients = runtime.local_app_config.llm.client_configs()
    name = memory.background_client_name or runtime.local_app_config.llm.default_client
    client = next((item for item in clients if item.name == name), None) if name else (clients[0] if clients else None)
    if memory.background_client_name and client is None:
        raise HTTPException(422, detail="unknown background client")
    if memory.background_model and (
        len(memory.background_model) > 200 or client is None
        or memory.background_model not in [client.default_model, *client.available_models]
    ):
        raise HTTPException(422, detail="background model must be configured on the selected client")
    if memory.background_client_name and len(memory.background_client_name) > 100:
        raise HTTPException(422, detail="background client name is too long")


@router.get("/background/config")
def get_background_config(request: Request):
    runtime = request.app.state.runtime
    state = runtime.memory_settings_store.read()
    desired = _merged(runtime, state["overrides"])
    active = {section: getattr(runtime.local_app_config, section).model_dump(mode="json")
              for section in ("memory", "background")}
    pending = [f"{section}.{key}" for section in active for key, value in active[section].items()
               if desired[section][key] != value]
    return {"revision": state["revision"], "updated_at": state["updated_at"],
            "active": active, "desired": desired, "overrides": state["overrides"],
            "restart_required": bool(pending), "pending_restart_fields": pending,
            "apply_mode": "restart", "config_load_error":
                "invalid_saved_configuration" if state["invalid"] else runtime.memory_settings_error}


@router.get("/background/config/schema")
def background_config_schema():
    """Form constraints only; provider credentials are outside this contract."""
    return {"memory": MemoryConfig.model_json_schema(),
            "background": BackgroundConfig.model_json_schema(), "apply_mode": "restart"}


@router.patch("/background/config")
def patch_background_config(payload: SettingsPatch, request: Request):
    runtime = request.app.state.runtime
    state = runtime.memory_settings_store.read()
    if state["revision"] != payload.expected_revision:
        raise HTTPException(409, detail="settings_revision_conflict")
    overrides = {key: dict(value) for key, value in state["overrides"].items()}
    for section, model in (("memory", MemoryConfig), ("background", BackgroundConfig)):
        changes = getattr(payload, section)
        if set(changes) - model.model_fields.keys():
            raise HTTPException(422, detail=f"unknown {section} configuration field")
        if changes:
            overrides.setdefault(section, {}).update(changes)
    _validate(runtime, _merged(runtime, overrides))
    try:
        runtime.memory_settings_store.save(overrides, expected_revision=payload.expected_revision)
    except SettingsConflictError as exc:
        raise HTTPException(409, detail=str(exc)) from exc
    return get_background_config(request)


@router.delete("/background/config")
def reset_background_config(request: Request, expected_revision: int = Query(ge=0)):
    try:
        request.app.state.runtime.memory_settings_store.save({}, expected_revision=expected_revision)
    except SettingsConflictError as exc:
        raise HTTPException(409, detail=str(exc)) from exc
    return get_background_config(request)
