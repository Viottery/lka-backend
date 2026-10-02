"""Desktop UI defaults and the configured LLM model catalog."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Literal

import httpx
from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

router = APIRouter(prefix="/agent", tags=["agent"])
_model_cache: dict[str, tuple[float, list[str]]] = {}


class UIDefaults(BaseModel):
    model_config = ConfigDict(extra="forbid")

    llm_client: str = Field(default="", max_length=100)
    llm_model: str = Field(default="", max_length=200)
    safety_mode: Literal["backend", "skip", "llm", "manual"] = "backend"
    stream: bool = True
    workspace_parent: str = Field(default="", max_length=1000)


@router.get("/ui-defaults")
def get_ui_defaults(request: Request) -> dict:
    with request.app.state.runtime._conn() as conn:
        row = conn.execute("SELECT preferences, updated_at FROM agent_ui_preferences WHERE id = 1").fetchone()
    if row is None:
        return {"configured": False, "defaults": UIDefaults().model_dump(), "updated_at": None}
    return {"configured": True, "defaults": UIDefaults.model_validate_json(row["preferences"]).model_dump(), "updated_at": row["updated_at"]}


@router.put("/ui-defaults")
def put_ui_defaults(payload: UIDefaults, request: Request) -> dict:
    timestamp = datetime.now(UTC).isoformat()
    with request.app.state.runtime._conn() as conn:
        conn.execute(
            "INSERT INTO agent_ui_preferences(id, preferences, updated_at) VALUES(1, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET preferences = excluded.preferences, updated_at = excluded.updated_at",
            (payload.model_dump_json(), timestamp),
        )
    return {"configured": True, "defaults": payload.model_dump(), "updated_at": timestamp}


async def _fetch_provider_model_ids(client: object) -> list[str]:
    url = str(client.base_url).rstrip("/") + "/models"
    async with httpx.AsyncClient(timeout=4.0) as http:
        response = await http.get(url, headers={"Authorization": "Bearer " + str(client.api_key)})
        response.raise_for_status()
        payload = response.json()
    items = payload.get("data", []) if isinstance(payload, dict) else []
    return list(dict.fromkeys(item["id"] for item in items if isinstance(item, dict) and isinstance(item.get("id"), str) and 0 < len(item["id"]) <= 200))[:500]


@router.get("/models")
async def list_agent_models(request: Request, refresh: bool = False) -> dict:
    service = request.app.state.runtime.agent_llm_client
    if service is None:
        return {"default_client": None, "clients": []}
    clients = []
    for client in service.registry.list_clients():
        configured = list(dict.fromkeys([client.default_model, *getattr(client, "available_models", [])]))
        models = configured
        source = "configured"
        if getattr(client, "base_url", None) and getattr(client, "api_key", None):
            cache_key = client.name + "|" + client.base_url
            cached = _model_cache.get(cache_key)
            if refresh or cached is None or time.monotonic() - cached[0] > 300:
                try:
                    discovered = await _fetch_provider_model_ids(client)
                    if discovered:
                        cached = (time.monotonic(), discovered)
                        _model_cache[cache_key] = cached
                except (httpx.HTTPError, ValueError, KeyError, TypeError):
                    pass
            if cached and time.monotonic() - cached[0] <= 300:
                models = list(dict.fromkeys([*configured, *cached[1]]))
                source = "provider"
        clients.append({"name": client.name, "default_model": client.default_model, "models": models, "source": source})
    return {"default_client": service.config.default_client or (clients[0]["name"] if clients else None), "clients": clients}
