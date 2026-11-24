"""Provider gateway: the ONLY path to LLM calls.

- Budget gate before every call (estimate → reserve), reconciliation after.
- Structured output validated against Pydantic schemas.
- One schema-repair retry (schema errors returned to the same model), then fail.
- Stub mode: deterministic local responses for tests/demo — clearly labeled.
- Stores every rendered prompt (secrets redacted) and every model call.
"""
from __future__ import annotations

import hashlib
import json
import random
import time
from dataclasses import dataclass
from typing import Any, Type

from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session

from config import get_config
from models import Handoff, ModelCall, RenderedPrompt
from security import redact_secrets
from services import budgets


class ProviderError(RuntimeError):
    pass


class SchemaValidationFailed(RuntimeError):
    def __init__(self, message: str, attempts: list[dict]):
        super().__init__(message)
        self.attempts = attempts


class CancellationRequested(RuntimeError):
    pass


def approx_tokens(text: str) -> int:
    return max(1, len(text) // 4)


@dataclass
class CallContext:
    session: Session
    project_id: str
    agent_role: str
    experiment_id: str | None = None
    interview_run_id: str | None = None
    experiment_cap_usd: float | None = None
    experiment_spent_usd: float = 0.0
    run_cap_usd: float | None = None
    run_spent_usd: float = 0.0
    is_holdout: bool = False
    cancellation_check: Any = None  # callable -> raises CancellationRequested
    seed: int | None = None


@dataclass
class CallResult:
    text: str
    parsed: BaseModel | None
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    latency_ms: int
    model_call_id: str
    provider_mode: str = "stub"
    retry_number: int = 0


# ----------------------------------------------------------------- stub mode

def _stub_hash(messages: list[dict], seed: int | None) -> int:
    payload = json.dumps(messages, sort_keys=True, ensure_ascii=False) + str(seed or 0)
    return int(hashlib.sha256(payload.encode()).hexdigest()[:8], 16)


def _stub_response(messages: list[dict], schema: Type[BaseModel] | None,
                   seed: int | None) -> str:
    """Deterministic stub. Produces schema-valid JSON for known schemas."""
    from schemas import (EvaluatorOutputSchema, InterviewerTurnSchema,
                         MemoryDeltaSchema, PairwiseJudgeSchema, ReportSchema,
                         SimulatorResponseSchema, CriticReviewSchema,
                         ResearchDecisionSchema, MutationSchema)
    rng = random.Random(_stub_hash(messages, seed))
    joined = " ".join(m.get("content", "") for m in messages)

    if schema is SimulatorResponseSchema:
        styles = [["specific", "confident"], ["vague"], ["verbose", "confident"], ["hesitant"]]
        answers = [
            "In my last role I led the migration of our billing service; I personally "
            "designed the retry queue and we cut failed payments by about 12 percent.",
            "Hmm, we did a lot of things as a team... it was mostly about collaboration, "
            "I would say communication was key in that project.",
            "So that project — honestly it was a huge effort. I was involved in almost "
            "everything, the architecture, the database, the frontend, everything really.",
            "I'm not fully sure about the exact numbers, but I remember I owned the "
            "integration tests and the deployment scripts for that release.",
        ]
        idx = rng.randrange(len(answers))
        return json.dumps({
            "spoken_answer": answers[idx],
            "used_truth_fact_ids": [f"fact-{rng.randrange(1, 9)}"],
            "used_exaggeration_ids": ["claim-1"] if idx == 2 else [],
            "response_style_tags": styles[idx],
            "simulator_consistency_warnings": [],
        })
    if schema is InterviewerTurnSchema:
        questions = [
            ("Tell me about the last project you shipped — what part was yours, specifically?", "ownership", False),
            ("You mentioned the migration — what broke in production, and how did you find it?", "technical_depth", True),
            ("Walk me through a week where priorities changed under you. What did you do first?", "adaptability", False),
            ("That result — how was it measured, and who verified it?", "evidence", True),
            ("What's the worst property of the approach you chose there?", "trade_offs", True),
            ("Think of a disagreement with a teammate that got tense. How did it start?", "collaboration", False),
        ]
        q, topic, follow = questions[rng.randrange(len(questions))]
        return json.dumps({
            "question": q, "reaction": "That's useful context." if follow else "",
            "internal_target": topic, "is_follow_up": follow, "topic": topic,
            "end_interview": False, "evidence_gap_targeted": topic})
    if schema is EvaluatorOutputSchema:
        metric_keys = []
        # Metric keys are passed in the prompt as a JSON list marker
        marker = "METRIC_KEYS_JSON:"
        if marker in joined:
            try:
                metric_keys = json.loads(joined.split(marker, 1)[1].split("\n", 1)[0])
            except Exception:
                metric_keys = []
        if not metric_keys:
            metric_keys = ["question_relevance", "follow_up_depth", "evidence_quality"]
        scores = []
        for key in metric_keys:
            base = 55 + rng.randrange(0, 35)
            scores.append({
                "metric_key": key, "score": base, "confidence": round(0.65 + rng.random() * 0.3, 2),
                "evidence_turn_ids": [f"turn-{rng.randrange(0, 4)}"],
                "reason": f"Stub deterministic assessment of {key}.", "failure_tags": []})
        return json.dumps({
            "scores": scores, "hard_constraint_findings": [], "overall_strengths": ["stub"],
            "overall_failures": [], "uncertainties": ["stub-mode output"],
            "requires_adjudication": False})
    if schema is PairwiseJudgeSchema:
        winner = ["A", "B", "tie"][rng.randrange(3)]
        return json.dumps({
            "winner_by_group": {"adaptive_quality": winner}, "overall_winner": winner,
            "evidence": ["Stub comparison."], "confidence": 0.7,
            "hard_constraint_differences": []})
    if schema is MemoryDeltaSchema:
        return json.dumps({
            "facts": {}, "skills": [{"name": "python", "probed_depth": 1}],
            "strengths": [], "risks": [], "contradictions": [],
            "asked_questions": [], "coverage_updates": {"ownership_delivery": 0.4},
            "remove": []})
    if schema is ReportSchema:
        return json.dumps({
            "summary": "Stub interview report: candidate showed moderate evidence of delivery.",
            "competency_findings": [{"competency": "ownership_delivery", "level": "moderate",
                                     "evidence_turn_ids": ["turn-1"]}],
            "strengths": ["clear examples"], "risks": ["unverified metrics"],
            "claims": [{"claim": "Candidate led a billing migration",
                        "evidence_turn_ids": ["turn-0"], "basis": "candidate_claims"}],
            "recommendation": "Decision support only — requires human review.",
            "recommendation_basis": "observed_evidence", "requires_human_review": True})
    if schema is CriticReviewSchema:
        return json.dumps({
            "causal_claim_issues": [], "leakage_concerns": [], "control_weaknesses": [],
            "overfitting_risks": ["small synthetic persona set"], "conflicting_metrics": [],
            "verdict": "upheld", "rationale": "Stub critic found no blocking issues."})
    if schema is ResearchDecisionSchema:
        return json.dumps({
            "decision": "run_experiment",
            "research_question": "Does evidence-gap follow-up policy improve follow-up depth?",
            "rationale": "Stub director selects the highest-value untested component.",
            "candidate_parent_architectures": [], "target_components": ["follow_up_policy"],
            "suggested_scenario_tags": ["development"], "maximum_budget": 1.0,
            "stopping_reason": None})
    if schema is MutationSchema:
        return json.dumps({
            "parent_architecture_id": "", "hypothesis_id": "",
            "mutation_scope": "single_component", "target_component": "follow_up_policy",
            "before_version": None, "proposed_after": {"follow_up_policy": "evidence_gap_tracking"},
            "reason": "Stub mutation", "expected_metric_changes": [],
            "risk_assessment": ["stub"], "requires_human_approval": False})
    # Unknown schema: synthesize a default-valid instance when possible
    if schema is not None:
        try:
            return schema().model_dump_json()
        except Exception:
            pass
    # free text
    return "Stub free-text response."


# ----------------------------------------------------------------- main call

_openai_client = None


def _get_openai_client():
    global _openai_client
    if _openai_client is None:
        from openai import OpenAI
        cfg = get_config()
        if not cfg.openai_api_key:
            raise ProviderError(
                "OPENAI_API_KEY is not set but PROVIDER_MODE=openai. "
                "Set the key in .env or use PROVIDER_MODE=stub.")
        _openai_client = OpenAI(api_key=cfg.openai_api_key, timeout=60.0)
    return _openai_client


def validate_model_available(model: str) -> None:
    cfg = get_config()
    if cfg.provider_mode == "stub":
        return
    try:
        client = _get_openai_client()
        client.models.retrieve(model)
    except Exception as exc:
        raise ProviderError(
            f"Configured model '{model}' is not available from the provider. "
            f"Fix OPENAI_*_MODEL in .env. Provider said: {redact_secrets(str(exc))[:300]}")


def _raw_call(messages: list[dict], model: str, schema: Type[BaseModel] | None,
              max_output_tokens: int, seed: int | None) -> tuple[str, int, int, str]:
    """Returns (text, input_tokens, output_tokens, provider_mode)."""
    cfg = get_config()
    if cfg.provider_mode == "stub":
        text = _stub_response(messages, schema, seed)
        return text, sum(approx_tokens(m.get("content", "")) for m in messages), approx_tokens(text), "stub"
    client = _get_openai_client()
    kwargs: dict = dict(model=model, messages=messages)
    is_reasoning = any(x in model for x in ("gpt-5.6", "sol", "terra", "luna"))
    if is_reasoning:
        kwargs["max_completion_tokens"] = max_output_tokens
    else:
        kwargs["max_tokens"] = max_output_tokens
        kwargs["temperature"] = 0.4
    if seed is not None:
        kwargs["seed"] = seed
    if schema is not None:
        kwargs["response_format"] = {"type": "json_object"}
    response = client.chat.completions.create(**kwargs)
    print(f"RAW OPENAI RESPONSE: {response}", flush=True)
    text = response.choices[0].message.content or ""
    usage = response.usage
    return text, usage.prompt_tokens, usage.completion_tokens, "openai"


def call_llm(ctx: CallContext, messages: list[dict], model: str,
             schema: Type[BaseModel] | None = None,
             max_output_tokens: int = 900,
             handoff_to: str | None = None) -> CallResult:
    """Budget-gated, schema-validated, cancellation-aware LLM call."""
    cfg = get_config()
    is_reasoning = any(x in model for x in ("gpt-5.6", "sol", "terra", "luna"))
    if is_reasoning and max_output_tokens < 4000:
        max_output_tokens = 4000

    if ctx.cancellation_check:
        ctx.cancellation_check()  # before the call

    est_input = sum(approx_tokens(m.get("content", "")) for m in messages)
    est_usd = budgets.estimate_call_cost(ctx.session, model, est_input, max_output_tokens)
    reservation = budgets.check_and_reserve(
        ctx.session, ctx.project_id, est_usd, scope_type="agent_call",
        scope_id=ctx.interview_run_id or ctx.experiment_id or "adhoc",
        experiment_cap_usd=ctx.experiment_cap_usd, experiment_spent_usd=ctx.experiment_spent_usd,
        run_cap_usd=ctx.run_cap_usd, run_spent_usd=ctx.run_spent_usd,
        is_holdout=ctx.is_holdout)

    attempts: list[dict] = []
    parsed: BaseModel | None = None
    text = ""
    retry = 0
    in_tokens = out_tokens = 0
    provider_mode = cfg.provider_mode
    started = time.time()
    try:
        while True:
            text, in_tokens, out_tokens, provider_mode = _raw_call(
                messages, model, schema, max_output_tokens, ctx.seed)
            if schema is None:
                break
            try:
                payload = json.loads(text)
                parsed = schema.model_validate(payload)
                break
            except (json.JSONDecodeError, ValidationError) as exc:
                print(f"SCHEMA VALIDATION ERROR FOR AGENT '{ctx.agent_role}' (retry {retry}):\nError: {exc}\nRaw Response:\n{text}\n", flush=True)
                attempts.append({"response": text[:4000], "errors": str(exc)[:2000]})
                if retry >= 1:
                    raise SchemaValidationFailed(
                        f"Agent '{ctx.agent_role}' produced invalid output twice for "
                        f"schema {schema.__name__}.", attempts)
                retry += 1
                messages = messages + [
                    {"role": "assistant", "content": text},
                    {"role": "user", "content":
                        "Your previous response failed schema validation with these errors:\n"
                        + str(exc)[:1500]
                        + "\nReturn ONLY corrected JSON matching the schema. Do not invent "
                          "missing protected data; use empty values where data is unknown."}]
    except SchemaValidationFailed:
        # Tokens were genuinely consumed — charge them honestly, mark the failure.
        if in_tokens or out_tokens:
            budgets.record_actual(
                ctx.session, ctx.project_id, reservation, model, in_tokens, out_tokens,
                agent_role=ctx.agent_role, experiment_id=ctx.experiment_id,
                interview_run_id=ctx.interview_run_id)
        else:
            budgets.release_reservation(ctx.session, reservation)
        ctx.session.add(ModelCall(
            interview_run_id=ctx.interview_run_id, experiment_id=ctx.experiment_id,
            agent_role=ctx.agent_role, model=model, provider_mode=provider_mode,
            input_tokens=in_tokens, output_tokens=out_tokens, latency_ms=0,
            cost_usd=0.0, seed=ctx.seed, retry_number=retry, status="schema_error"))
        ctx.session.flush()
        raise
    except Exception:
        budgets.release_reservation(ctx.session, reservation)
        raise
    latency_ms = int((time.time() - started) * 1000)

    budgets.record_actual(
        ctx.session, ctx.project_id, reservation, model, in_tokens, out_tokens,
        agent_role=ctx.agent_role, experiment_id=ctx.experiment_id,
        interview_run_id=ctx.interview_run_id)

    call_row = ModelCall(
        interview_run_id=ctx.interview_run_id, experiment_id=ctx.experiment_id,
        agent_role=ctx.agent_role, model=model, provider_mode=provider_mode,
        input_tokens=in_tokens, output_tokens=out_tokens, latency_ms=latency_ms,
        cost_usd=budgets.estimate_call_cost(ctx.session, model, in_tokens, out_tokens),
        seed=ctx.seed, retry_number=retry, status="ok")
    ctx.session.add(call_row)
    ctx.session.flush()
    ctx.session.add(RenderedPrompt(
        model_call_id=call_row.id, agent_role=ctx.agent_role,
        messages_json=[{"role": m["role"], "content": redact_secrets(m.get("content", ""))}
                       for m in messages]))
    if handoff_to and parsed is not None:
        schema_name = next((k for k, v in __import__("schemas").SCHEMA_REGISTRY.items()
                            if v is schema), schema.__name__ if schema else "free_text")
        ctx.session.add(Handoff(
            run_id=ctx.interview_run_id, experiment_id=ctx.experiment_id,
            from_agent=ctx.agent_role, to_agent=handoff_to, schema_name=schema_name,
            payload_json=json.loads(parsed.model_dump_json()),
            validation_status="valid", retry_number=retry))
    ctx.session.flush()
    if ctx.cancellation_check:
        ctx.cancellation_check()  # after the call

    return CallResult(text=text, parsed=parsed, model=model, input_tokens=in_tokens,
                      output_tokens=out_tokens, cost_usd=call_row.cost_usd,
                      latency_ms=latency_ms, model_call_id=call_row.id,
                      provider_mode=provider_mode, retry_number=retry)
