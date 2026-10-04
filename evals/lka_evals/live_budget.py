"""Persistent, conservative accounting for explicitly authorized live evaluations.

This is evaluation infrastructure, not application pricing policy. Reservations
survive crashes; unknown usage and failed HTTP calls never become free retries.
"""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

from app.core.llm.errors import LLMClientError
from app.core.llm.models import LLMRequest, LLMStreamEvent
from app.core.prompt_tokens import PromptTokenCounter


class LiveBudgetExceeded(LLMClientError):
    """No external dispatch is allowed after a reservation is refused."""


class ProtectedEvaluationContent(LLMClientError):
    """Credential content is never authorized as model-visible test input."""


def _protected_credential_values(env_file: Path = Path(".env")) -> tuple[str, ...]:
    pairs = list(os.environ.items())
    full_env_text = ""
    if env_file.is_file():
        full_env_text = env_file.read_text(encoding="utf-8").strip()
        for line in full_env_text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            name, value = stripped.removeprefix("export ").split("=", 1)
            pairs.append((name.strip(), value.strip().strip('"').strip("'")))
    credentials = {value for name, value in pairs
        if re.search(r"(?:^|_)(?:API_KEY|TOKEN|SECRET|PASSWORD|PASSWD)(?:$|_)", name.upper())
        and not name.upper().endswith(("_ENV", "_PATH", "_FILE")) and len(value) >= 8}
    if full_env_text:
        credentials.add(full_env_text)
    return tuple(sorted(credentials))


class LiveBudget:
    def __init__(self, path: Path, *, usd_limit: float = 50, call_limit: int = 2000,
                 search_limit: int = 3000) -> None:
        if not math.isfinite(usd_limit) or usd_limit <= 0 or call_limit < 1 or search_limit < 0:
            raise ValueError("invalid live evaluation budget")
        self.path = path
        self.usd_limit, self.call_limit, self.search_limit = usd_limit, call_limit, search_limit
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS calls(
                id TEXT PRIMARY KEY, kind TEXT NOT NULL, stage TEXT NOT NULL,
                status TEXT NOT NULL, reserved REAL NOT NULL, charged REAL NOT NULL,
                input_tokens INTEGER, output_tokens INTEGER, cached_tokens INTEGER,
                usage_known INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")

    def connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=30)

    @staticmethod
    def cost(incoming: int, outgoing: int, cached: int = 0) -> float:
        return ((incoming - cached) * .8 + cached * .016 + outgoing * 3.2) / 1_000_000

    def reserve(self, *, kind: str, stage: str, incoming: int = 0, outgoing: int = 0) -> str:
        if kind not in {"llm", "search"} or incoming < 0 or outgoing < 0:
            raise ValueError("invalid live reservation")
        call_id = uuid4().hex
        reserved = self.cost(incoming, outgoing) if kind == "llm" else 0
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            used, calls = conn.execute(
                "SELECT COALESCE(SUM(charged),0),COUNT(*) FROM calls WHERE kind='llm'"
            ).fetchone()
            searches = conn.execute("SELECT COUNT(*) FROM calls WHERE kind='search'").fetchone()[0]
            if (kind == "llm" and (used + reserved > self.usd_limit or calls >= self.call_limit)
                    or kind == "search" and searches >= self.search_limit):
                raise LiveBudgetExceeded("live evaluation budget exhausted before dispatch")
            conn.execute("INSERT INTO calls(id,kind,stage,status,reserved,charged) VALUES(?,?,?,?,?,?)",
                         (call_id, kind, stage, "reserved", reserved, reserved))
        return call_id

    def finish(self, call_id: str, usage: dict | None, *, status: str) -> None:
        usage = usage or {}
        incoming = usage.get("prompt_tokens", usage.get("input_tokens"))
        outgoing = usage.get("completion_tokens", usage.get("output_tokens"))
        known = all(type(value) is int and value >= 0 for value in (incoming, outgoing))
        details = usage.get("prompt_tokens_details") or {}
        cached = usage.get("prompt_cache_hit_tokens", details.get("cached_tokens", 0)
                           if isinstance(details, dict) else 0)
        if type(cached) is not int or cached < 0 or (known and cached > incoming):
            cached = 0
        with self.connect() as conn:
            row = conn.execute("SELECT status,reserved FROM calls WHERE id=?", (call_id,)).fetchone()
            if row is None or row[0] != "reserved":
                return
            conn.execute("""UPDATE calls SET status=?,charged=?,input_tokens=?,output_tokens=?,
                         cached_tokens=?,usage_known=? WHERE id=?""",
                         (status, self.cost(incoming, outgoing, cached) if known else row[1],
                          incoming if known else None, outgoing if known else None,
                          cached if known else None, int(known), call_id))

    def snapshot(self) -> dict:
        with self.connect() as conn:
            rows = conn.execute("""SELECT kind,COUNT(*),COALESCE(SUM(charged),0),
                SUM(usage_known),COALESCE(SUM(input_tokens),0),COALESCE(SUM(output_tokens),0),
                COALESCE(SUM(cached_tokens),0) FROM calls GROUP BY kind""").fetchall()
        groups = {r[0]: {"calls": r[1], "charged_usd": r[2], "known_usage_calls": r[3],
                        "input_tokens": r[4], "output_tokens": r[5], "cached_tokens": r[6]} for r in rows}
        return {"usd_limit": self.usd_limit, "call_limit": self.call_limit,
                "search_limit": self.search_limit, "groups": groups,
                "pricing": {"input": .8, "output": 3.2, "cache_read": .016}}


class MeteredClient:
    """Wrap the provider boundary, covering every consumer of the LLM service."""

    def __init__(self, client, budget: LiveBudget, *, allowed_model: str,
                 protected_values: tuple[str, ...] | None = None) -> None:
        self.client, self.budget, self.allowed_model = client, budget, allowed_model
        self.protected_values = (_protected_credential_values() if protected_values is None
                                 else protected_values)

    def __getattr__(self, name):
        return getattr(self.client, name)

    def _reserve(self, request: LLMRequest) -> tuple[LLMRequest, str]:
        payload = json.dumps(request.model_dump(mode="json"), ensure_ascii=False)
        if any(value and value in payload for value in self.protected_values):
            raise ProtectedEvaluationContent("protected credential content refused before dispatch")
        if (request.model or self.client.default_model) != self.allowed_model:
            raise LiveBudgetExceeded("unpriced model refused by evaluation ledger")
        # Bound only absent caps; this live probe limit is not a production setting.
        request = request.model_copy(update={"max_output_tokens": request.max_output_tokens or 16384})
        counter = PromptTokenCounter()
        incoming = sum(counter.count_text(m.content).count + 8 for m in request.messages)
        incoming += len(json.dumps([t.model_dump(mode="json") for t in request.tools],
                                   ensure_ascii=False).encode("utf-8")) + 4096
        call_id = self.budget.reserve(kind="llm", stage=request.prompt_summary,
                                      incoming=incoming, outgoing=request.max_output_tokens)
        return request, call_id

    async def complete(self, request):
        request, call_id = self._reserve(request)
        try:
            response = await self.client.complete(request)
        except BaseException:
            self.budget.finish(call_id, None, status="failed_or_cancelled")
            raise
        self.budget.finish(call_id, response.usage, status=response.status)
        return response

    async def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        request, call_id = self._reserve(request)
        usage = None
        status = "incomplete"
        try:
            async for event in self.client.stream(request):
                if isinstance(event.metadata.get("usage"), dict):
                    usage = event.metadata["usage"]
                if event.event_type in {"llm_completed", "llm_failed"}:
                    status = event.event_type
                yield event
        finally:
            self.budget.finish(call_id, usage, status=status)


def instrument_service(service, budget: LiveBudget, *, allowed_model: str) -> None:
    if service is None:
        raise RuntimeError("live evaluation requires a configured real LLM service")
    for client in service.registry.list_clients():
        service.registry.register_client(MeteredClient(client, budget, allowed_model=allowed_model))
