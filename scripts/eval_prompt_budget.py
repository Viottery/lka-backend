"""Calibrate local prompt counts against optional provider usage.

All fixtures are synthetic. Remote calls require --remote and use the current
configured model; neither prompts nor credentials are printed or persisted.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import statistics
import time
from dataclasses import dataclass
from pathlib import Path

from app.core.llm import LLMMessage, LLMRequest, LLMToolDefinition, build_llm_service
from app.core.local_config import load_local_config
from app.core.prompt_tokens import PromptTokenCounter


@dataclass(frozen=True)
class ProbeCase:
    name: str
    system: str
    user: str
    tools: list[LLMToolDefinition]


def fixture_cases() -> list[ProbeCase]:
    """Reproducible multilingual, structured and schema-heavy samples."""

    rng = random.Random(20261002)
    chinese = "".join(chr(0x4E00 + rng.randrange(0x5000)) for _ in range(4000))
    structured = json.dumps(
        [{"id": index, "message": f"item-{index}", "valid": index % 2 == 0}
         for index in range(150)], ensure_ascii=False,
    )
    tools = [
        LLMToolDefinition(
            name=f"lookup_{index}", description="Search synthetic records only.",
            parameters={
                "type": "object", "properties": {
                    "query": {"type": "string", "description": "Synthetic query"},
                    "page": {"type": "integer", "minimum": 1},
                }, "required": ["query"],
            },
        ) for index in range(12)
    ]
    return [
        ProbeCase("chinese", "Reply briefly.", chinese, []),
        ProbeCase("json", "Reply briefly.", structured, []),
        ProbeCase("unicode", "Reply briefly.", "🧭🚀✨📮" * 500, []),
        ProbeCase("tool_schemas", "Reply briefly; do not call tools.",
                  "This is a synthetic schema-counting check.", tools),
    ]


def local_records(counter: PromptTokenCounter) -> list[dict[str, object]]:
    return [
        {
            "case": case.name,
            "input_estimate": counted.count,
            "count_method": counted.method,
            "conservative": counted.conservative,
        }
        for case in fixture_cases()
        for counted in [counter.count_request(case.system, case.user, case.tools)]
    ]


async def provider_records(config_path: Path, local: list[dict[str, object]]) -> list[dict[str, object]]:
    config = load_local_config(config_path)
    service = build_llm_service(config.llm)
    if service is None:
        raise RuntimeError("No configured remote LLM service")
    results: list[dict[str, object]] = []
    for case, estimated in zip(fixture_cases(), local, strict=True):
        if case.tools and not service.supports_function_calling():
            results.append({
                **estimated, "status": "skipped",
                "reason": "configured client does not send native tool schemas",
            })
            continue
        started = time.perf_counter()
        try:
            response = await service.complete(LLMRequest(
                messages=[
                    LLMMessage(role="system", content=case.system),
                    LLMMessage(role="user", content=case.user),
                ],
                prompt_summary=f"synthetic_prompt_budget_probe_{case.name}",
                max_output_tokens=16,
                tools=case.tools,
            ))
            actual = response.usage.get("prompt_tokens") or response.usage.get("input_tokens")
            if not isinstance(actual, int) or actual <= 0:
                raise RuntimeError("Provider did not return input token usage")
            results.append({
                **estimated, "actual_input_tokens": actual,
                "actual_over_estimate": round(actual / int(estimated["input_estimate"]), 4),
                "duration_ms": round((time.perf_counter() - started) * 1000),
                "status": "ok",
            })
        except Exception as exc:  # noqa: BLE001 - report category without prompt or response body.
            results.append({
                **estimated, "status": "failed", "error_type": type(exc).__name__,
                "duration_ms": round((time.perf_counter() - started) * 1000),
            })
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config/local.toml"))
    parser.add_argument("--remote", action="store_true", help="Send synthetic fixtures to configured LLM")
    args = parser.parse_args()
    config = load_local_config(args.config)
    client = config.llm.client_configs()[0] if config.llm.client_configs() else None
    model = config.llm.resolve_model_config(client.name, client.default_model) if client else None
    path = model.tokenizer_json_path if model else None
    counter = PromptTokenCounter(path) if path and path.is_file() else PromptTokenCounter()
    local = local_records(counter)
    rows = asyncio.run(provider_records(args.config, local)) if args.remote else local
    for row in rows:
        print(json.dumps(row, ensure_ascii=False, sort_keys=True))
    ratios = [float(row["actual_over_estimate"]) for row in rows
              if row.get("status") == "ok"]
    if ratios:
        print(json.dumps({
            "successful_cases": len(ratios), "total_cases": len(rows),
            "max_actual_over_estimate": max(ratios),
            "median_actual_over_estimate": statistics.median(ratios),
        }, sort_keys=True))


if __name__ == "__main__":
    main()
