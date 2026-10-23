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
        has_skill = p.get("has_required_skill")
        why.append("skill requirement unknown" if has_skill is None else "has required skill" if has_skill else "lacks required skill")
        why.append("home ward" if p.get("home_ward") == data["ward"] else f"floats from {p.get('home_ward')}")
        why.append(f"{p.get('week_hours')}h this week")
        candidates.append({"action": "offer_shift_to_staff", "params": {"staff_id": p["staff_id"]}, "reason": ", ".join(why)})
    return json.dumps({"candidates": candidates})


def _rule_replan(data: dict[str, Any]) -> str:
    """Use the Verifier's refusal reasons: skip staff whose World Model facts show the
    same problem (a heuristic on facts; the Verifier still checks every proposal)."""
    refused = {c for r in data.get("refusals", []) for c in r.get("failed_checks", [])}
    tried = set(data.get("already_tried", []))
    shift_hours = float(data.get("shift_hours") or 0)

    def avoids(p: dict[str, Any]) -> bool:
        if p["staff_id"] in tried or p.get("already_on_this_shift"):
            return False
        if "staff_not_on_leave" in refused and p.get("leave_on_date"):
            return False
        if "has_required_skill" in refused and p.get("has_required_skill") is False:
            return False
        if "under_weekly_hour_cap" in refused and float(p.get("week_hours") or 0) + shift_hours > float(p.get("weekly_hour_cap") or 0):
            return False
        if "no_conflicting_shift" in refused and p.get("shifts_on_date"):
            return False
        return True

    pool = sorted((p for p in data["staff_pool"] if avoids(p)),
                  key=lambda p: (not p.get("has_required_skill"), p.get("home_ward") != data["ward"], float(p.get("week_hours") or 0), p["staff_id"]))
    reasons = sorted(refused)
    return json.dumps({"candidates": [
        {"action": "offer_shift_to_staff", "params": {"staff_id": p["staff_id"]},
         "reason": "avoids earlier refusals: " + ", ".join(reasons)}
        for p in pool[: data.get("max_offers", 4)]
    ]})


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
    "replan_candidates": _rule_replan,
    "investigate": _rule_investigate,
}


def rule_based_generator(prompt: str, system_prompt: str) -> str:
    match = TASK_TAG.search(system_prompt)
    task = match.group(1) if match else ""
    if task not in _RULE_TASKS:
        return "{}"
    return _RULE_TASKS[task](task_input(prompt))


# --- live provider --------------------------------------------------------------


class EmptyReply(RuntimeError):
    """The model returned no usable content (e.g. reasoning used up the token budget)."""


METERLESS_APP_ID = "preflight"  # sent as the meterless-app header on every request


def normalize_meterless_url(url: str) -> str:
    """Any pasted Nightshift URL -> <host>/v1 (contract: never /v1/v1)."""
    u = url.strip().rstrip("/")
    if "://" not in u:
        u = "http://" + u
    for suffix in ("/chat/completions", "/models", "/healthz"):
        if u.endswith(suffix):
            u = u[: -len(suffix)]
    u = u.rstrip("/")
    while u.endswith("/v1/v1"):
        u = u[:-3]
    return u if u.endswith("/v1") else u + "/v1"


def _chat_generator(base_url: str, api_key: str, model: str, timeout: float, max_tokens: int,
                    extra_body: dict | None = None, headers: dict | None = None) -> Generator:
    from openai import OpenAI

    client = OpenAI(base_url=base_url, api_key=api_key, timeout=timeout, max_retries=1, default_headers=headers or None)

    def generate(prompt: str, system_prompt: str) -> str:
        reply = client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=max_tokens,
            extra_body=extra_body,
        )
        choice = reply.choices[0]
        text = THINK_BLOCK.sub("", choice.message.content or "").strip()
        if not text:
            raise EmptyReply(f"empty reply from {model} (finish_reason={choice.finish_reason})")
        return text

    return generate


def meterless_generator(cfg: Config) -> Generator:
    """Primary provider: Meterless Nightshift runtime. Bearer runtime token, meterless-app
    header, routing-mode model id (meterless/auto by default), plain text requests."""
    return _chat_generator(
        normalize_meterless_url(cfg.llm_base_url), cfg.meterless_token, cfg.meterless_model,
        cfg.llm_timeout_s, cfg.llm_max_tokens,
        headers={"meterless-app": METERLESS_APP_ID, "meterless-scope": "user"},
    )


def nvidia_generator(cfg: Config) -> Generator:
    """Documented fallback: NVIDIA's OpenAI-compatible endpoint, called directly."""
    # Nemotron reasoning models spend the whole budget on hidden reasoning for
    # these short structured tasks unless thinking is switched off.
    extra_body = None if cfg.llm_reasoning else {"chat_template_kwargs": {"enable_thinking": False}}
    return _chat_generator(cfg.nvidia_base_url, cfg.llm_api_key, cfg.llm_model, min(cfg.llm_timeout_s, 60),
                           cfg.llm_max_tokens, extra_body=extra_body)


openai_compatible_generator = nvidia_generator  # backwards-compatible name


def provider_status(cfg: Config, client=None) -> dict[str, Any]:
    """Meterless contract: GET /v1/models decides. 200 with meterless/* ids -> online,
    401/403 -> unauthorized, anything else -> offline. Never returns the token."""
    import httpx

    url = normalize_meterless_url(cfg.llm_base_url)
    headers = {"Authorization": f"Bearer {cfg.meterless_token}", "meterless-app": METERLESS_APP_ID}
    try:
        r = (client or httpx).get(f"{url}/models", headers=headers, timeout=10)
    except Exception as exc:  # connection refused, timeout, DNS
        return {"provider": "meterless", "base_url": url, "status": "offline", "detail": type(exc).__name__, "models": []}
    if r.status_code in (401, 403):
        return {"provider": "meterless", "base_url": url, "status": "unauthorized", "detail": f"HTTP {r.status_code}", "models": []}
    try:
        ids = [m.get("id", "") for m in r.json().get("data", [])] if r.status_code == 200 else []
    except ValueError:
        ids = []
    status = "online" if any(i.startswith("meterless/") for i in ids) else "offline"
    return {"provider": "meterless", "base_url": url, "status": status, "detail": f"HTTP {r.status_code}", "models": ids}


def with_fallback(primary: Generator, fallback: Generator, on_fallback: Callable[[str], None]) -> Generator:
    def generate(prompt: str, system_prompt: str) -> str:
        try:
            return primary(prompt, system_prompt)
        except Exception as exc:  # any provider failure -> deterministic fallback
            on_fallback(f"{type(exc).__name__}: {str(exc)[:200]}")
            return fallback(prompt, system_prompt)

    return generate


def make_generator(cfg: Config, on_fallback: Callable[[str], None]) -> tuple[Generator, str]:
    """Live: Meterless Nightshift first, then NVIDIA direct (documented fallback), then rules.
    SAMPLE_MODE always uses the rule-based generator."""
    if cfg.sample_mode:
        return rule_based_generator, "rule-based"
    chain: list[tuple[str, Generator]] = []
    if cfg.meterless_token:
        if "nvidia.com" in cfg.llm_base_url:
            from app.config import ConfigError

            raise ConfigError("LLM_BASE_URL points at NVIDIA but METERLESS_RUNTIME_TOKEN is set: point LLM_BASE_URL at "
                              "Nightshift (default http://127.0.0.1:8088/v1); NVIDIA direct uses NVIDIA_BASE_URL.")
        chain.append((f"meterless:{cfg.meterless_model}", meterless_generator(cfg)))
    if cfg.llm_configured:
        chain.append((f"nvidia-direct:{cfg.llm_model}", nvidia_generator(cfg)))
    if not chain:
        return rule_based_generator, "rule-based"
    generator: Generator = rule_based_generator
    for name, gen in reversed(chain):
        generator = with_fallback(gen, generator, lambda note, n=name: on_fallback(f"{n} failed: {note}"))
    fallbacks = [n.split(":")[0] for n, _ in chain[1:]] + ["rule-based"]
    return generator, f"{chain[0][0]} (fallback: {' -> '.join(fallbacks)})"
