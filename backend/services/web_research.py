"""Web research tool for pipeline stages.

Lets a pipeline stage research the company, the field, or questioning
techniques BEFORE or DURING an interview — so the tournament can test whether
web-informed pipelines interview better.

Modes (env WEB_RESEARCH_MODE, defaults to PROVIDER_MODE):
- "stub": deterministic fake research results, clearly labeled — free, for tests.
- "openai": uses the OpenAI Responses API `web_search` tool when the account/
  model supports it; fails with a clear setup error otherwise (never silently
  fakes results in real mode).

Security: everything returned is wrapped as UNTRUSTED data before it can reach
any interviewer prompt, and scanned for prompt-injection patterns. Web calls
go through the same budget gateway as every other model call.
"""
from __future__ import annotations

import hashlib
import os
import random

from sqlalchemy.orm import Session

from config import get_config
from models import AuditEvent
from security import detect_injection, redact_secrets, wrap_untrusted
from services import budgets
from services.provider import approx_tokens


class WebResearchError(RuntimeError):
    pass


def _mode() -> str:
    return os.environ.get("WEB_RESEARCH_MODE", get_config().provider_mode).strip().lower()


def _stub_result(query: str) -> str:
    seed = int(hashlib.sha256(query.encode()).hexdigest()[:8], 16)
    rng = random.Random(seed)
    templates = [
        ("Industry overview: the field around '{q}' currently emphasizes practical, "
         "evidence-based skill verification. Common interview practice is to anchor "
         "questions in the candidate's real delivered work rather than abstract quizzes."),
        ("Company/role research for '{q}': typical teams in this space value ownership, "
         "measurable outcomes, and cross-functional communication. Deal-breaker skills "
         "are usually probed first."),
        ("Questioning technique for '{q}': research-backed approaches include "
         "past-behavioral openers, STAR completion probes, and one-level-deeper "
         "trade-off questions ('why X over the obvious alternative? what broke?')."),
    ]
    picked = rng.sample(templates, k=2)
    return "\n\n".join(t.format(q=query) for t in picked) + \
        "\n\n[STUB WEB RESEARCH — deterministic local placeholder, not real web data]"


def _openai_search(query: str, model: str) -> str:
    from openai import OpenAI
    cfg = get_config()
    if not cfg.openai_api_key:
        raise WebResearchError(
            "WEB_RESEARCH_MODE=openai but OPENAI_API_KEY is not set.")
    client = OpenAI(api_key=cfg.openai_api_key)
    try:
        response = client.responses.create(
            model=model,
            tools=[{"type": "web_search"}],
            input=(f"Search the web and summarize (max ~300 words, factual, with "
                   f"source names) the most useful current information for: {query}"))
        text = getattr(response, "output_text", None) or ""
        if not text:
            raise WebResearchError("Provider returned an empty web-search response.")
        return text
    except WebResearchError:
        raise
    except Exception as exc:
        raise WebResearchError(
            "Web search is not available with the configured model/account. "
            f"Set WEB_RESEARCH_MODE=stub or fix the setup. Provider said: "
            f"{redact_secrets(str(exc))[:300]}")


def research(session: Session, project_id: str, query: str, purpose: str,
             agent_role: str = "web_researcher",
             experiment_id: str | None = None,
             interview_run_id: str | None = None,
             cancellation_check=None) -> dict:
    """Run one budget-gated web research query. Returns
    {"wrapped": untrusted-wrapped text for prompts, "raw": text,
     "injection_signals": [...], "mode": str}."""
    if cancellation_check:
        cancellation_check()
    mode = _mode()
    cfg = get_config()
    model = cfg.low_cost_model or cfg.interview_model
    query = str(query or "")[:500]

    est_usd = budgets.estimate_call_cost(session, model, approx_tokens(query) + 600, 500)
    reservation = budgets.check_and_reserve(
        session, project_id, est_usd, scope_type="agent_call",
        scope_id=interview_run_id or experiment_id or "web_research")
    try:
        if mode == "stub":
            text = _stub_result(query)
            in_tokens, out_tokens = approx_tokens(query), approx_tokens(text)
        else:
            text = _openai_search(query, model)
            in_tokens, out_tokens = approx_tokens(query) + 600, approx_tokens(text)
    except Exception:
        budgets.release_reservation(session, reservation)
        raise
    budgets.record_actual(session, project_id, reservation, model,
                          in_tokens, out_tokens, agent_role=agent_role,
                          experiment_id=experiment_id,
                          interview_run_id=interview_run_id)
    injection_signals = detect_injection(text)
    session.add(AuditEvent(
        actor=agent_role, action="web_research",
        subject_type="interview_run" if interview_run_id else "experiment",
        subject_id=interview_run_id or experiment_id or "",
        detail_json={"query": query, "purpose": purpose, "mode": mode,
                     "chars": len(text), "injection_signals": injection_signals}))
    session.flush()
    if cancellation_check:
        cancellation_check()
    return {
        "wrapped": wrap_untrusted(text, f"WEB RESEARCH ({purpose})"),
        "raw": text,
        "injection_signals": injection_signals,
        "mode": mode,
    }
