"""Opt-in synthetic image probe; at most three remote calls, no private source files."""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import secrets
import sys
from pathlib import Path
from time import perf_counter
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PIL import Image, ImageDraw, ImageFont

from app.core.llm import build_llm_service
from app.core.llm.models import LLMImageBlock, LLMImageURL, LLMMessage, LLMRequest, LLMTextBlock
from app.core.llm_workloads import LLMWorkloadController, workload_scope
from app.core.local_config import load_local_config
from app.core.tools import ToolContext, ToolInvocation
from app.domains.documents import DocumentService
from app.tool_packages.filesystem import FileAccessPolicy
from app.tool_packages.vision import InspectImageTool

QUESTION = ('Read the image and return only a JSON object with keys code (printed heading), '
            'red_squares (count), blue_circles (count), row_values (table numbers in row order), '
            'total (sum of table numbers). Do not infer values from file names.')


def make_case(path: Path) -> dict:
    expected = {"code": "LKA-" + secrets.token_hex(4).upper(),
                "red_squares": secrets.randbelow(4) + 1,
                "blue_circles": secrets.randbelow(4) + 1,
                "row_values": [secrets.randbelow(80) + 11 for _ in range(3)]}
    expected["total"] = sum(expected["row_values"])
    font = ImageFont.load_default(size=28)
    image = Image.new("RGB", (760, 640), "white")
    draw = ImageDraw.Draw(image)
    draw.text((35, 20), expected["code"], font=font, fill="black")
    for index in range(expected["red_squares"]):
        x = 40 + index * 110
        draw.rectangle((x, 100, x + 60, 160), fill="red")
    for index in range(expected["blue_circles"]):
        x = 40 + index * 110
        draw.ellipse((x, 210, x + 60, 270), fill="blue")
    for index, number in enumerate(expected["row_values"]):
        y = 330 + index * 80
        draw.rectangle((35, y, 550, y + 75), outline="black", width=2)
        draw.text((55, y + 18), f"Row {index + 1}", font=font, fill="black")
        draw.text((350, y + 18), str(number), font=font, fill="black")
    image.save(path)
    image.close()
    return expected


def score(content: str, expected: dict) -> dict:
    text = content.strip()
    if text.startswith("```"):
        text = "\n".join(text.splitlines()[1:-1])
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return {"passed": False, "json_valid": False}
    checks = {key: isinstance(value, dict) and type(value.get(key)) is type(answer)
              and value.get(key) == answer for key, answer in expected.items()}
    return {"passed": all(checks.values()), "json_valid": True, "checks": checks}


def image_request(path: Path, *, client_name: str, model: str, thinking: bool | None) -> LLMRequest:
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return LLMRequest(client_name=client_name, model=model, prompt_summary="Synthetic visual API probe",
                      max_output_tokens=512, thinking_enabled=thinking,
                      messages=[LLMMessage(role="user", content=[LLMTextBlock(text=QUESTION),
                                LLMImageBlock(image_url=LLMImageURL(url="data:image/png;base64," + encoded))])])


async def collect_stream(service, request: LLMRequest) -> dict:
    events = [event async for event in service.stream(request)]
    last = events[-1]
    return {"status": last.metadata.get("status", "completed") if last.event_type == "llm_completed" else "failed",
            "partial": last.metadata.get("partial", False), "content": last.content_snapshot,
            "usage": last.metadata.get("usage", {}),
            "finish_reason": last.metadata.get("finish_reason"), "events": len(events)}


def case_passed(row: dict) -> bool:
    return (row["status"] == "completed" and not row.get("partial")
            and row.get("finish_reason") not in {"length", "max_tokens"} and row["score"]["passed"])


def run_probe(config_path: Path, output: Path, client_name: str | None = None) -> dict:
    if output.exists():
        raise ValueError("Probe output directory must be new; existing artifacts are preserved")
    output.mkdir(parents=True)
    config = load_local_config(config_path).llm.model_copy(deep=True)
    configs = config.client_configs()
    selected = next((client for client in configs if client.name == (client_name or config.default_client)), configs[0] if configs else None)
    if selected is None or (client_name is not None and selected.name != client_name):
        raise ValueError("Requested model client is unavailable")
    # Permit only this exact model in this in-memory probe. Persistent configuration is untouched.
    selected = selected.model_copy(deep=True)
    selected.vision_model_overrides[selected.default_model] = True
    selected.timeout_seconds = min(selected.timeout_seconds, 45)
    config.clients = [selected]
    config.default_client = selected.name
    service = build_llm_service(config)
    if service is None:
        raise ValueError("A real configured model service is required")
    service.workloads = LLMWorkloadController(output / "usage.sqlite3")
    report = {"client": selected.name, "model": selected.default_model,
              "endpoint_host": urlparse(selected.base_url).hostname,
              "limits": {"max_calls": 3, "output_tokens_per_call": 512, "task_token_reservation": 120000},
              "synthetic_only": True, "persistent_config_changed": False, "cases": []}
    thinking = False if selected.thinking_control == "deepseek" else None
    for mode in ("complete", "pdf_tool", "stream"):
        path = output / f"{mode}.png"
        expected = make_case(path)
        started = perf_counter()
        row = {"mode": mode, "expected": expected, "image_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        try:
            with workload_scope("interactive", task_id="synthetic-vision-probe", max_tokens=120000):
                if mode == "pdf_tool":
                    pdf_path = output / "scanned.pdf"
                    with Image.open(path) as image:
                        image.save(pdf_path, "PDF", resolution=120)
                    documents = DocumentService()
                    opened = documents.open(pdf_path)
                    read = documents.read_pages(pdf_path, opened["sha256"])
                    row["pdf_without_text"] = read["pages"][0]["needs_visual"]
                    rendered = documents.render_page(pdf_path, opened["sha256"], page=1,
                                                     output_path=output / "rendered.png", dpi=120)
                    tool = InspectImageTool(FileAccessPolicy.from_workspace_roots([output]), service)
                    # Disable provider thinking for this bounded experiment without changing tool defaults.
                    original = service.complete
                    async def bounded(request, original=original):
                        return await original(request.model_copy(update={"thinking_enabled": thinking}))
                    service.complete = bounded
                    try:
                        result = tool.invoke(invocation=ToolInvocation(invocation_id="synthetic-vision", tool=tool.spec,
                            session_id="probe", context_id="probe", input={"path": rendered["output_path"],
                            "expected_sha256": rendered["image_sha256"], "question": QUESTION, "max_output_tokens": 512}),
                            context=ToolContext(session_id="probe", safety_review_approved=True))
                    finally:
                        service.complete = original
                    row.update(status=result.status, content=result.output.get("content", ""),
                               audit=result.output.get("llm_call", {}), error=result.error)
                elif mode == "complete":
                    response = asyncio.run(service.complete(image_request(path, client_name=selected.name, model=selected.default_model, thinking=thinking)))
                    row.update(status=response.status, content=response.content, usage=response.usage,
                               finish_reason=response.finish_reason, partial=response.partial)
                else:
                    request = image_request(path, client_name=selected.name, model=selected.default_model, thinking=thinking)
                    row.update(asyncio.run(asyncio.wait_for(collect_stream(service, request), timeout=60)))
            row["score"] = score(row.get("content", ""), expected)
            row["passed"] = case_passed(row)
        except Exception as exc:  # noqa: BLE001 - private transport errors must never echo credentials/image data.
            row.update(status="failed", passed=False, error_type=type(exc).__name__,
                       http_status=getattr(exc, "status_code", None), category=getattr(exc, "error_category", None))
        row["duration_seconds"] = round(perf_counter() - started, 3)
        report["cases"].append(row)
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({key: row.get(key) for key in ("mode", "status", "passed", "duration_seconds", "http_status", "category")}), flush=True)
        if not row["passed"]:
            break
    report["passed"] = len(report["cases"]) == 3 and all(row["passed"] for row in report["cases"])
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="Explicitly permit up to three paid model calls")
    parser.add_argument("--config", type=Path, default=ROOT / "config/local.toml")
    parser.add_argument("--client")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not args.run:
        print("No requests sent. Use --run --output NEW_DIRECTORY; maximum three synthetic image calls, 512 output tokens each.")
        return 0
    if args.output is None:
        parser.error("--output is required with --run")
    return 0 if run_probe(args.config, args.output.resolve(), args.client)["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
