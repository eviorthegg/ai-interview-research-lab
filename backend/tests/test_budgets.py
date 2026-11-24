"""Budget ledger and cost gateway unit tests (critical safety)."""
import pytest


def _make_project(session, total=10.0, holdout=2.0):
    from models import Budget, ResearchProject
    project = ResearchProject(name="t")
    session.add(project)
    session.flush()
    session.add(Budget(project_id=project.id, total_usd=total,
                       holdout_reserve_usd=holdout))
    session.flush()
    return project.id


def test_cost_calculation_uses_versioned_pricing(lab):
    from db import session_scope
    from services import budgets
    with session_scope() as session:
        pricing = budgets.get_active_pricing(session)
        usd = budgets.price_tokens(pricing.pricing_json, "gpt-4o-mini",
                                   1_000_000, 1_000_000)
        assert usd == pytest.approx(0.15 + 0.60)


def test_budget_cannot_exceed_project_cap(lab):
    from db import session_scope
    from services import budgets
    with session_scope() as session:
        pid = _make_project(session, total=1.0, holdout=0.0)
        budgets.check_and_reserve(session, pid, 0.9, "run", "r1")
        with pytest.raises(budgets.BudgetExceededError):
            budgets.check_and_reserve(session, pid, 0.2, "run", "r2")


def test_holdout_reserve_cannot_be_consumed(lab):
    from db import session_scope
    from services import budgets
    with session_scope() as session:
        pid = _make_project(session, total=10.0, holdout=8.0)
        with pytest.raises(budgets.BudgetExceededError):
            budgets.check_and_reserve(session, pid, 5.0, "run", "r1")
        # holdout runs may use it
        budgets.check_and_reserve(session, pid, 5.0, "run", "r2", is_holdout=True)


def test_holdout_unlock_allows_spend(lab):
    from db import session_scope
    from services import budgets
    with session_scope() as session:
        pid = _make_project(session, total=10.0, holdout=8.0)
        budgets.unlock_holdout(session, pid)
        budgets.check_and_reserve(session, pid, 5.0, "run", "r1")


def test_reconciliation_and_atomic_update(lab):
    from db import session_scope
    from services import budgets
    with session_scope() as session:
        pid = _make_project(session)
        res = budgets.check_and_reserve(session, pid, 0.5, "agent_call", "x")
        budget = budgets.get_budget(session, pid)
        assert budget.reserved_usd == pytest.approx(0.5)
        budgets.record_actual(session, pid, res, "gpt-4o-mini", 1000, 500)
        budget = budgets.get_budget(session, pid)
        assert budget.reserved_usd == pytest.approx(0.0)
        assert budget.spent_usd > 0


def test_idempotent_cost_event_no_duplicate_charge(lab):
    from db import session_scope
    from services import budgets
    with session_scope() as session:
        pid = _make_project(session)
        res1 = budgets.check_and_reserve(session, pid, 0.5, "agent_call", "x")
        e1 = budgets.record_actual(session, pid, res1, "gpt-4o-mini", 1000, 500,
                                   idempotency_key="k1")
        spent_after_first = budgets.get_budget(session, pid).spent_usd
        res2 = budgets.check_and_reserve(session, pid, 0.5, "agent_call", "x")
        e2 = budgets.record_actual(session, pid, res2, "gpt-4o-mini", 1000, 500,
                                   idempotency_key="k1")
        assert e1.id == e2.id
        assert budgets.get_budget(session, pid).spent_usd == pytest.approx(spent_after_first)


def test_experiment_and_run_caps(lab):
    from db import session_scope
    from services import budgets
    with session_scope() as session:
        pid = _make_project(session)
        with pytest.raises(budgets.BudgetExceededError):
            budgets.check_and_reserve(session, pid, 0.5, "agent_call", "x",
                                      run_cap_usd=0.4, run_spent_usd=0.0)
        with pytest.raises(budgets.BudgetExceededError):
            budgets.check_and_reserve(session, pid, 0.5, "agent_call", "x",
                                      experiment_cap_usd=1.0, experiment_spent_usd=0.8)
