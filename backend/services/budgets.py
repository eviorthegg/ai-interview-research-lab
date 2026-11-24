"""Budget ledger and cost gateway.

Every provider call goes through check_and_reserve() BEFORE the call and
record_actual() AFTER. Pricing comes from the versioned model_pricing_versions
table — never hardcoded in orchestration logic.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from db import new_uuid
from models import (Budget, BudgetReservation, CostEvent, Experiment,
                    InterviewRun, ModelPricingVersion)

DEFAULT_PRICING = {
    # USD per 1M tokens. Versioned research decision, editable via pricing table.
    "gpt-5.6-sol": {"input_per_m": 5.00, "output_per_m": 30.00, "cached_per_m": 2.50},
    "gpt-5.6-terra": {"input_per_m": 2.50, "output_per_m": 15.00, "cached_per_m": 1.25},
    "gpt-5.6-luna": {"input_per_m": 1.00, "output_per_m": 6.00, "cached_per_m": 0.50},
    "gpt-5.6": {"input_per_m": 5.00, "output_per_m": 15.00, "cached_per_m": 2.50},
    "gpt-4o": {"input_per_m": 2.50, "output_per_m": 10.00, "cached_per_m": 1.25},
    "gpt-4o-mini": {"input_per_m": 0.15, "output_per_m": 0.60, "cached_per_m": 0.075},
    "text-embedding-3-small": {"input_per_m": 0.02, "output_per_m": 0.0, "cached_per_m": 0.0},
    "stub-model": {"input_per_m": 1.00, "output_per_m": 3.00, "cached_per_m": 0.5},
}


class BudgetExceededError(RuntimeError):
    pass


@dataclass
class CostEstimate:
    input_tokens: int
    output_tokens: int
    usd: float


def get_active_pricing(session: Session) -> ModelPricingVersion:
    row = session.execute(
        select(ModelPricingVersion).where(ModelPricingVersion.active == True)  # noqa: E712
        .order_by(ModelPricingVersion.version.desc())).scalars().first()
    if row is None:
        row = ModelPricingVersion(version=1, active=True, pricing_json=DEFAULT_PRICING)
        session.add(row)
        session.flush()
    return row


def price_tokens(pricing: dict, model: str, input_tokens: int, output_tokens: int,
                 cached_tokens: int = 0) -> float:
    rates = pricing.get(model)
    if rates is None:
        # Unknown model: use the most expensive configured rate (conservative).
        rates = max(pricing.values(), key=lambda r: r.get("input_per_m", 0)) if pricing else \
            {"input_per_m": 15.0, "output_per_m": 60.0, "cached_per_m": 7.5}
    uncached = max(0, input_tokens - cached_tokens)
    usd = (uncached * rates.get("input_per_m", 0)
           + cached_tokens * rates.get("cached_per_m", rates.get("input_per_m", 0))
           + output_tokens * rates.get("output_per_m", 0)) / 1_000_000
    return round(usd, 8)


def estimate_call_cost(session: Session, model: str, input_tokens: int,
                       output_tokens: int) -> float:
    pricing = get_active_pricing(session)
    return price_tokens(pricing.pricing_json, model, input_tokens, output_tokens)


def get_budget(session: Session, project_id: str) -> Budget:
    budget = session.execute(
        select(Budget).where(Budget.project_id == project_id)).scalars().first()
    if budget is None:
        raise BudgetExceededError(f"No budget configured for project {project_id}")
    return budget


def _available(budget: Budget, include_holdout: bool) -> float:
    reserve = 0.0 if (include_holdout or budget.holdout_unlocked) else budget.holdout_reserve_usd
    return budget.total_usd - budget.spent_usd - budget.reserved_usd - reserve


def check_and_reserve(session: Session, project_id: str, amount_usd: float,
                      scope_type: str, scope_id: str,
                      experiment_cap_usd: float | None = None,
                      experiment_spent_usd: float = 0.0,
                      run_cap_usd: float | None = None,
                      run_spent_usd: float = 0.0,
                      is_holdout: bool = False) -> BudgetReservation:
    """Atomically check every budget level and reserve. Raises BudgetExceededError."""
    budget = get_budget(session, project_id)
    if run_cap_usd is not None and run_spent_usd + amount_usd > run_cap_usd:
        raise BudgetExceededError(
            f"Run cap {run_cap_usd:.4f} would be exceeded ({run_spent_usd:.4f} + {amount_usd:.4f})")
    if experiment_cap_usd is not None and experiment_spent_usd + amount_usd > experiment_cap_usd:
        raise BudgetExceededError(
            f"Experiment cap {experiment_cap_usd:.4f} would be exceeded "
            f"({experiment_spent_usd:.4f} + {amount_usd:.4f})")
    if _available(budget, include_holdout=is_holdout) < amount_usd:
        raise BudgetExceededError(
            f"Project budget exhausted: available "
            f"{_available(budget, include_holdout=is_holdout):.4f} < {amount_usd:.4f} "
            f"(holdout reserve {'unlocked' if budget.holdout_unlocked else 'locked'})")
    reservation = BudgetReservation(
        budget_id=budget.id, scope_type=scope_type, scope_id=scope_id,
        amount_usd=amount_usd, status="active")
    budget.reserved_usd = round(budget.reserved_usd + amount_usd, 8)
    budget.version += 1
    session.add(reservation)
    session.flush()
    return reservation


def record_actual(session: Session, project_id: str, reservation: BudgetReservation,
                  model: str, input_tokens: int, output_tokens: int,
                  cached_tokens: int = 0, agent_role: str = "",
                  experiment_id: str | None = None,
                  interview_run_id: str | None = None,
                  idempotency_key: str | None = None) -> CostEvent:
    """Reconcile estimate vs actual and update all levels atomically."""
    if idempotency_key:
        existing = session.execute(select(CostEvent).where(
            CostEvent.idempotency_key == idempotency_key)).scalars().first()
        if existing:
            return existing  # duplicate charge prevented
    pricing = get_active_pricing(session)
    actual = price_tokens(pricing.pricing_json, model, input_tokens, output_tokens, cached_tokens)
    budget = get_budget(session, project_id)
    budget.reserved_usd = round(max(0.0, budget.reserved_usd - reservation.amount_usd), 8)
    budget.spent_usd = round(budget.spent_usd + actual, 8)
    budget.version += 1
    reservation.status = "consumed"
    event = CostEvent(
        project_id=project_id, experiment_id=experiment_id,
        interview_run_id=interview_run_id, agent_role=agent_role, model=model,
        input_tokens=input_tokens, output_tokens=output_tokens,
        cached_tokens=cached_tokens, estimated_usd=reservation.amount_usd,
        actual_usd=actual, pricing_version_id=pricing.id,
        idempotency_key=idempotency_key or new_uuid())
    session.add(event)
    if experiment_id:
        exp = session.get(Experiment, experiment_id)
        if exp:
            exp.actual_cost_usd = round(exp.actual_cost_usd + actual, 8)
    if interview_run_id:
        run = session.get(InterviewRun, interview_run_id)
        if run:
            run.total_cost_usd = round(run.total_cost_usd + actual, 8)
            run.total_input_tokens += input_tokens
            run.total_output_tokens += output_tokens
            run.llm_call_count += 1
    session.flush()
    return event


def release_reservation(session: Session, reservation: BudgetReservation) -> None:
    if reservation.status == "active":
        budget = session.get(Budget, reservation.budget_id)
        budget.reserved_usd = round(max(0.0, budget.reserved_usd - reservation.amount_usd), 8)
        budget.version += 1
        reservation.status = "released"
        session.flush()


def unlock_holdout(session: Session, project_id: str, actor: str = "human") -> None:
    budget = get_budget(session, project_id)
    budget.holdout_unlocked = True
    budget.version += 1
    session.flush()
