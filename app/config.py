"""Runtime configuration, read once from the environment.

Secrets live in .env.local (gitignored). Values already set in the process
environment win over the file, so `SAMPLE_MODE=true python -m app ...` works.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
SAMPLE_TODAY = date(2026, 10, 26)  # the synthetic data is anchored to this Monday


class ConfigError(RuntimeError):
    """Configuration is missing or inconsistent; the agent must not start."""


def _flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Config:
    sample_mode: bool
    sample_scenario: str
    today: date
    logs_dir: Path
    state_dir: Path
    data_dir: Path

    zetaris_rest_url: str
    zetaris_org_id: str
    zetaris_api_key: str
    zetaris_query_limit: int
    zetaris_table_prefix: str
    zetaris_timeout_s: float

    llm_api_key: str
    llm_model: str
    llm_base_url: str
    llm_timeout_s: float

    @property
    def llm_configured(self) -> bool:
        return bool(self.llm_api_key and self.llm_model)


def load_config(**overrides) -> Config:
    load_dotenv(ROOT / ".env.local", override=False)
    env = os.environ.get
    demo_today = (env("DEMO_TODAY") or "").strip()
    sample_mode = overrides.get("sample_mode", _flag("SAMPLE_MODE", True))
    if "today" in overrides:
        today = overrides["today"]
    elif demo_today:
        today = date.fromisoformat(demo_today)
    elif sample_mode:
        today = SAMPLE_TODAY  # never the system clock: results are identical on any date
    else:
        raise ConfigError(
            "Live mode needs DEMO_TODAY (YYYY-MM-DD) in .env.local. The demo data loaded into Zetaris "
            f"is anchored to {SAMPLE_TODAY.isoformat()}, so set DEMO_TODAY={SAMPLE_TODAY.isoformat()} "
            "for the demo, or to the real date when running against live hospital data."
        )
    cfg = dict(
        sample_mode=sample_mode,
        sample_scenario=env("SAMPLE_SCENARIO") or "scenario_a",
        today=today,
        logs_dir=Path(env("PREFLIGHT_LOGS_DIR") or ROOT / "logs"),
        state_dir=Path(env("PREFLIGHT_STATE_DIR") or ROOT / "state"),
        data_dir=ROOT / "data" / "sample",
        zetaris_rest_url=(env("ZETARIS_REST_URL") or "http://localhost:8888/api/v1.0").rstrip("/"),
        zetaris_org_id=env("ZETARIS_ORG_ID") or "",
        zetaris_api_key=env("ZETARIS_API_KEY") or "",
        zetaris_query_limit=int(env("ZETARIS_QUERY_LIMIT") or 1000),
        zetaris_table_prefix=env("ZETARIS_TABLE_PREFIX") or "preflight",
        zetaris_timeout_s=float(env("ZETARIS_TIMEOUT_S") or 15),
        llm_api_key=env("NVIDIA_API_KEY") or "",
        llm_model=env("NVIDIA_MODEL") or "",
        llm_base_url=env("LLM_BASE_URL") or "https://integrate.api.nvidia.com/v1",
        llm_timeout_s=float(env("LLM_TIMEOUT_S") or 30),
    )
    cfg.update(overrides)
    return Config(**cfg)
