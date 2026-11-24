"""Configuration for the Evior Interview Research Lab.

All settings come from environment variables (.env supported).
The application REFUSES to start research work unless production-write
isolation is guaranteed (RESEARCH_LAB_MODE=true, ALLOW_PRODUCTION_WRITES=false).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
LAB_ROOT = BASE_DIR.parent


def _load_dotenv() -> None:
    """Minimal .env loader (no external dependency required)."""
    for candidate in (LAB_ROOT / ".env", BASE_DIR / ".env"):
        if candidate.exists():
            for line in candidate.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip())
            break


_load_dotenv()


def _bool(name: str, default: str = "false") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes")


class IsolationError(RuntimeError):
    """Raised when production-write isolation cannot be guaranteed."""


@dataclass
class Config:
    # Provider
    openai_api_key: str = field(default_factory=lambda: os.environ.get("OPENAI_API_KEY", ""))
    interview_model: str = field(default_factory=lambda: os.environ.get("OPENAI_INTERVIEW_MODEL", "gpt-5.6-terra"))
    evaluator_model: str = field(default_factory=lambda: os.environ.get("OPENAI_EVALUATOR_MODEL", "gpt-5.6-sol"))
    low_cost_model: str = field(default_factory=lambda: os.environ.get("OPENAI_LOW_COST_MODEL", "gpt-5.6-luna"))
    embedding_model: str = field(default_factory=lambda: os.environ.get("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small"))
    # "openai" (real calls) or "stub" (deterministic local stub, used by tests)
    provider_mode: str = field(default_factory=lambda: os.environ.get("PROVIDER_MODE", "stub"))

    # Storage
    database_url: str = field(default_factory=lambda: os.environ.get(
        "DATABASE_URL", f"sqlite:///{(LAB_ROOT / 'evior_research_lab.db').as_posix()}"))

    # Isolation
    research_lab_mode: bool = field(default_factory=lambda: _bool("RESEARCH_LAB_MODE", "true"))
    allow_production_writes: bool = field(default_factory=lambda: _bool("ALLOW_PRODUCTION_WRITES", "false"))

    # Budget
    default_project_budget_usd: float = field(default_factory=lambda: float(os.environ.get("DEFAULT_PROJECT_BUDGET_USD", "100")))
    final_validation_reserve_usd: float = field(default_factory=lambda: float(os.environ.get("FINAL_VALIDATION_RESERVE_USD", "20")))

    # Execution
    max_concurrent_experiments: int = field(default_factory=lambda: int(os.environ.get("MAX_CONCURRENT_EXPERIMENTS", "1")))
    max_model_retries: int = field(default_factory=lambda: int(os.environ.get("MAX_MODEL_RETRIES", "2")))
    sse_heartbeat_seconds: int = field(default_factory=lambda: int(os.environ.get("SSE_HEARTBEAT_SECONDS", "15")))
    worker_heartbeat_seconds: int = field(default_factory=lambda: int(os.environ.get("WORKER_HEARTBEAT_SECONDS", "10")))
    job_abandon_timeout_seconds: int = field(default_factory=lambda: int(os.environ.get("JOB_ABANDON_TIMEOUT_SECONDS", "120")))
    log_level: str = field(default_factory=lambda: os.environ.get("LOG_LEVEL", "INFO"))

    # Autonomous loop safety limits (can be overridden via env vars)
    max_autonomous_rounds: int = field(default_factory=lambda: int(os.environ.get("MAX_AUTONOMOUS_ROUNDS", "20")))
    max_variants_per_round: int = field(default_factory=lambda: int(os.environ.get("MAX_VARIANTS_PER_ROUND", "4")))
    max_autonomous_experiments: int = field(default_factory=lambda: int(os.environ.get("MAX_AUTONOMOUS_EXPERIMENTS", "50")))

    # Paths to production artifacts (READ-ONLY)
    production_prompts_path: str = field(default_factory=lambda: os.environ.get(
        "EVIOR_PROMPTS_V3_PATH",
        str(LAB_ROOT.parent / "Backend-sam" / "sam-app" / "utility_layer" / "prompts" / "prompts_v3.json")))
    production_intro_outro_path: str = field(default_factory=lambda: os.environ.get(
        "EVIOR_INTRO_OUTRO_PATH",
        str(LAB_ROOT.parent / "Backend-sam" / "sam-app" / "utility_layer" / "prompts" / "Intro_outro.json")))

    def assert_isolation(self) -> None:
        if not self.research_lab_mode:
            raise IsolationError("RESEARCH_LAB_MODE must be true to run research experiments.")
        if self.allow_production_writes:
            raise IsolationError(
                "ALLOW_PRODUCTION_WRITES=true — the research lab refuses to start "
                "experiments unless production-write isolation is guaranteed.")
        if "sam-app" in self.database_url or "Frontend-react" in self.database_url:
            raise IsolationError("DATABASE_URL points inside production code paths.")


_config: Config | None = None


def get_config() -> Config:
    global _config
    if _config is None:
        _config = Config()
    return _config


def reset_config() -> None:
    """Test helper — re-read environment."""
    global _config
    _config = None
