"""The single LLM entry point: generator(prompt, system_prompt) -> text.

This is the Meterless "injected generator" pattern: every engine and agent step
that needs a model receives this function; nothing else in the code base calls
a model.

* Live: any OpenAI-compatible endpoint (default NVIDIA, https://integrate.api.nvidia.com/v1),
  model from NVIDIA_MODEL, key from NVIDIA_API_KEY. Point LLM_BASE_URL at a
  local server to keep prompts on the machine.
* SAMPLE_MODE or no key: rule_based_generator, a deterministic stand-in that
  answers the same task prompts.
* Live failure (timeout, HTTP error): falls back to the rule-based generator and
  reports it through `on_fallback`, so a run never stalls on the model.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable

from app.config import Config

Generator = Callable[[str, str], str]

TASK_TAG = re.compile(r"^TASK:\s*(\w+)", re.M)
INPUT_BLOCK = re.compile(r"<input>\s*(\{.*\})\s*</input>", re.S)
THINK_BLOCK = re.compile(r"<think>.*?</think>", re.S)


def extract_json(text: str) -> Any:
    """Parse the first JSON object in a model reply (tolerates prose and <think>)."""
    text = THINK_BLOCK.sub("", text)
    start = text.find("{")
    if start < 0:
        raise ValueError("no JSON object in reply")
    obj, _ = json.JSONDecoder().raw_decode(text[start:])
    return obj


def task_input(prompt: str) -> dict[str, Any]:
    match = INPUT_BLOCK.search(prompt)
    return json.loads(match.group(1)) if match else {}


# --- rule-based stand-in -------------------------------------------------------


def _rule_parse(data: dict[str, Any]) -> str:
    from app.request_parser import parse_with_rules

    return json.dumps(parse_with_rules(data["text"], data["today"]))


def _rule_plan(data: dict[str, Any]) -> str:
    """Rank internal staff by fit: free that day, required skill, home ward, fewest hours.

    Like any ranker it does not verify eligibility; that is the gate's job.
    Staff recommended by recalled memory go first.
    """
    remembered = [s for s in data.get("memory_staff_ids", [])]
    pool = [p for p in data["staff_pool"] if not p.get("already_on_this_shift")]

    def fit(p: dict[str, Any]) -> tuple:
        return (
            p["staff_id"] not in remembered,
            bool(p.get("shifts_on_date")),
            not p.get("has_required_skill"),
            p.get("home_ward") != data["ward"],
            float(p.get("week_hours") or 0),
            p["staff_id"],
        )

    ranked = sorted(pool, key=fit)[: data.get("max_offers", 4)]
    candidates = []
    for p in ranked:
        why = []
        if p["staff_id"] in remembered:
            why.append("covered this shift pattern before (memory)")
        why.append("already works that day" if p.get("shifts_on_date") else "free that day")
        why.append("has required skill" if p.get("has_required_skill") else "lacks required skill")
        why.append("home ward" if p.get("home_ward") == data["ward"] else f"floats from {p.get('home_ward')}")
        why.append(f"{p.get('week_hours')}h this week")
        candidates.append({"action": "offer_shift_to_staff", "params": {"staff_id": p["staff_id"]}, "reason": ", ".join(why)})
    return json.dumps({"candidates": candidates})


def _rule_investigate(data: dict[str, Any]) -> str:
    lines = [f"No action passed its checks for {data['shift']}. Do not book anyone automatically."]
    for attempt in data.get("attempts", []):
        failed = "; ".join(f"{c['name']}: {c['explanation']}" for c in attempt["failed"])
        lines.append(f"- {attempt['candidate']} refused: {failed}")
    if data.get("data_errors"):
        lines.append("Data could not be verified; check the Zetaris connection before acting.")
    lines.append("Recommended: the unit manager reviews the evidence above and decides manually.")
    return "\n".join(lines)


_RULE_TASKS: dict[str, Callable[[dict[str, Any]], str]] = {
    "parse_request": _rule_parse,
    "plan_candidates": _rule_plan,
    "investigate": _rule_investigate,
}


def rule_based_generator(prompt: str, system_prompt: str) -> str:
    match = TASK_TAG.search(system_prompt)
    task = match.group(1) if match else ""
    if task not in _RULE_TASKS:
        return "{}"
    return _RULE_TASKS[task](task_input(prompt))


# --- live provider --------------------------------------------------------------


def openai_compatible_generator(cfg: Config) -> Generator:
    from openai import OpenAI

    client = OpenAI(base_url=cfg.llm_base_url, api_key=cfg.llm_api_key, timeout=cfg.llm_timeout_s, max_retries=1)

    def generate(prompt: str, system_prompt: str) -> str:
        reply = client.chat.completions.create(
            model=cfg.llm_model,
            messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=1200,
        )
        return THINK_BLOCK.sub("", reply.choices[0].message.content or "").strip()

    return generate


def with_fallback(primary: Generator, fallback: Generator, on_fallback: Callable[[str], None]) -> Generator:
    def generate(prompt: str, system_prompt: str) -> str:
        try:
            return primary(prompt, system_prompt)
        except Exception as exc:  # any provider failure -> deterministic fallback
            on_fallback(f"{type(exc).__name__}: {str(exc)[:200]}")
            return fallback(prompt, system_prompt)

    return generate


def make_generator(cfg: Config, on_fallback: Callable[[str], None]) -> tuple[Generator, str]:
    if cfg.sample_mode or not cfg.llm_configured:
        return rule_based_generator, "rule-based"
    return with_fallback(openai_compatible_generator(cfg), rule_based_generator, on_fallback), f"openai-compatible:{cfg.llm_model}"
