"""Optional LLM-as-a-judge answer evaluator."""

from __future__ import annotations

import json
from typing import Any

from app.core.llm import LLMResponseMode


async def judge_answer(*, client: Any, question: str, answer: str, gold: str, evidence: list[str]) -> dict[str, Any]:
    prompt = json.dumps({"question": question, "gold_answer": gold, "agent_answer": answer, "evidence": evidence[:8]}, ensure_ascii=False)
    response = await client.complete_text(
        system_prompt=("You are an answer evaluator. Return JSON only: {\"score\":0..1,\"correct\":true/false,\"grounded\":true/false,\"reason\":\"short\"}. Judge factual correctness and evidence grounding; do not reward unsupported guesses."),
        user_prompt=prompt,
        prompt_summary="eval_answer_llm_judge",
        temperature=0.0,
        max_output_tokens=180,
        response_mode=LLMResponseMode.JSON,
        require_json=True,
        metadata={"eval_role": "answer_judge"},
    )
    try:
        value = json.loads(response.content)
        if not isinstance(value, dict): raise ValueError("judge output is not an object")
        score = max(0.0, min(1.0, float(value.get("score", 0.0))))
        return {"status": "completed", "score": score, "correct": bool(value.get("correct")), "grounded": bool(value.get("grounded")), "reason": str(value.get("reason") or ""), "model": response.model, "usage": response.usage}
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        return {"status": "invalid", "score": 0.0, "error": str(exc), "raw_preview": response.content[:500], "model": response.model, "usage": response.usage}
