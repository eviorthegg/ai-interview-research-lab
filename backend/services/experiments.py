"""Experiment service: specification validation, cost estimation, launch, decisions."""
from __future__ import annotations

import random

from sqlalchemy import select
from sqlalchemy.orm import Session

from models import (Architecture, ArchitectureVersion, BackgroundJob, Experiment,
                    ExperimentArchitecture, ExperimentScenario, IdempotencyKey,
                    InterviewRun, TimelineEvent)
from schemas import ExperimentDesignSchema
from services import budgets
from state_machine import transition

# Practical-significance defaults (configurable, versioned research decisions)
DEFAULT_PROMOTION_CRITERIA = {
    "min_quality_improvement_points": 3.0,
    "min_relative_deterministic_improvement": 0.05,
    "max_protected_dimension_regression_points": 5.0,
    "max_cost_increase_ratio": 0.25,
    "no_new_hard_constraint_failures": True,
}


class ExperimentValidationError(ValueError):
    pass


def validate_design(session: Session, design: ExperimentDesignSchema) -> list[str]:
    errors: list[str] = []
    control = session.get(ArchitectureVersion, design.control_architecture_version_id)
    variant = session.get(ArchitectureVersion, design.variant_architecture_version_id)
    if control is None:
        errors.append("Control architecture version not found.")
    if variant is None:
        errors.append("Variant architecture version not found.")
    if control and variant:
        if control.id == variant.id:
            errors.append("Control and variant must differ.")
        # Fair-comparison check: model config must be locked equal unless the
        # experiment explicitly targets model cost (bundled label).
        if (control.model_config_json != variant.model_config_json
                and "model" not in design.independent_variable.lower()
                and "cost" not in design.independent_variable.lower()):
            errors.append(
                "Unfair comparison: model configs differ but the independent "
                "variable does not concern model choice/cost.")
        diff_keys = [k for k in set(control.components_json) | set(variant.components_json)
                     if control.components_json.get(k) != variant.components_json.get(k)]
        if len(diff_keys) > 1 and "bundle" not in design.independent_variable.lower():
            errors.append(
                f"Variant changes multiple components {diff_keys} but is not labeled "
                "a bundled intervention (put 'bundled' in independent_variable).")
    if not design.scenario_persona_version_ids:
        errors.append("At least one candidate scenario is required.")
    if not design.dependent_metrics:
        errors.append("Dependent metrics are required.")
    if design.max_cost_usd <= 0:
        errors.append("max_cost_usd must be positive.")
    if not design.interview_modes:
        errors.append("At least one interview mode is required.")
    return errors


def estimate_cost(session: Session, design: ExperimentDesignSchema) -> dict:
    """Editable cost estimate based on current pricing configuration."""
    model = design.model_config_lock.get("interview_model", "gpt-5.6")
    eval_model = design.model_config_lock.get("evaluator_model", model)
    turns = int(design.model_config_lock.get("max_turns", 8))
    n_runs = len(design.scenario_persona_version_ids) * len(design.interview_modes) \
        * design.repetitions * 2  # control + variant
    # Per turn: interviewer call + simulator call.
    per_turn = (budgets.estimate_call_cost(session, model, 3000, 400)
                + budgets.estimate_call_cost(session, model, 2000, 350))
    per_run_interviews = per_turn * turns
    per_run_eval = (budgets.estimate_call_cost(session, eval_model, 5000, 1200) * 2  # QA + mode
                    + budgets.estimate_call_cost(session, eval_model, 6000, 600))    # pairwise share
    per_run_report = budgets.estimate_call_cost(session, model, 3000, 800)
    total = n_runs * (per_run_interviews + per_run_eval + per_run_report)
    return {
        "interview_runs": n_runs,
        "per_run_usd": round(per_run_interviews + per_run_eval + per_run_report, 4),
        "estimated_total_usd": round(total, 4),
        "assumptions": {
            "turns_per_interview": turns, "interview_model": model,
            "evaluator_model": eval_model,
            "note": "Estimate from versioned pricing table; reconciled after each call."},
    }


def create_experiment(session: Session, project_id: str, name: str,
                      design: ExperimentDesignSchema, code: str = "",
                      hypothesis_id: str | None = None,
                      is_template: bool = False,
                      research_run_id: str | None = None) -> Experiment:
    errors = validate_design(session, design)
    if errors and not is_template:
        raise ExperimentValidationError("; ".join(errors))
    experiment = Experiment(
        project_id=project_id, hypothesis_id=hypothesis_id or design.hypothesis_id or None,
        code=code, name=name, state="DRAFT",
        spec_json=design.model_dump(), is_template=is_template,
        research_run_id=research_run_id)
    session.add(experiment)
    session.flush()
    for arm, version_id in (("control", design.control_architecture_version_id),
                            ("variant", design.variant_architecture_version_id)):
        session.add(ExperimentArchitecture(
            experiment_id=experiment.id, architecture_version_id=version_id, arm=arm))
    for pv_id in design.scenario_persona_version_ids:
        for mode in design.interview_modes:
            session.add(ExperimentScenario(
                experiment_id=experiment.id, persona_version_id=pv_id,
                interview_mode=mode, repetitions=design.repetitions))
    session.flush()
    return experiment


def launch(session: Session, experiment: Experiment, idempotency_key: str | None = None) -> dict:
    """Validate → estimate → queue. Idempotent via Idempotency-Key."""
    if idempotency_key:
        existing = session.execute(select(IdempotencyKey).where(
            IdempotencyKey.key == idempotency_key)).scalars().first()
        if existing:
            return existing.response_json
    design = ExperimentDesignSchema.model_validate(experiment.spec_json)
    transition(session, experiment, "VALIDATING_CONFIGURATION", actor="api")
    errors = validate_design(session, design)
    if errors:
        transition(session, experiment, "FAILED", reason="; ".join(errors), actor="api")
        raise ExperimentValidationError("; ".join(errors))
    transition(session, experiment, "ESTIMATING_COST", actor="api")
    estimate = estimate_cost(session, design)
    experiment.estimated_cost_usd = estimate["estimated_total_usd"]
    if estimate["estimated_total_usd"] > design.max_cost_usd:
        transition(session, experiment, "BUDGET_BLOCKED",
                   reason=f"Estimate {estimate['estimated_total_usd']} exceeds cap "
                          f"{design.max_cost_usd}", actor="api")
        raise budgets.BudgetExceededError(
            f"Estimated cost ${estimate['estimated_total_usd']} exceeds experiment cap "
            f"${design.max_cost_usd}.")
    # Assign blind labels now (random, stored, never shown to evaluators)
    labels = ["A", "B"]
    random.shuffle(labels)
    arms = session.execute(select(ExperimentArchitecture).where(
        ExperimentArchitecture.experiment_id == experiment.id)).scalars().all()
    for arm, label in zip(arms, labels):
        arm.blind_label = label
    transition(session, experiment, "QUEUED", actor="api")
    job = BackgroundJob(job_type="run_experiment", subject_id=experiment.id,
                        payload_json={"experiment_id": experiment.id})
    session.add(job)
    session.add(TimelineEvent(
        project_id=experiment.project_id, event_type="experiment_launched",
        title=f"Experiment launched: {experiment.name}",
        detail_json={"experiment_id": experiment.id, "estimate": estimate}, actor="api"))
    session.flush()
    response = {"experiment_id": experiment.id, "state": experiment.state,
                "estimate": estimate, "job_id": job.id}
    if idempotency_key:
        session.add(IdempotencyKey(key=idempotency_key, scope="experiment_launch",
                                   response_json=response))
        session.flush()
    return response


def request_cancel(session: Session, experiment: Experiment) -> None:
    experiment.cancel_requested = True
    if experiment.state not in ("DRAFT", "COMPLETED", "CANCELLED", "FAILED"):
        try:
            transition(session, experiment, "CANCEL_REQUESTED", actor="api")
        except Exception:
            pass
    session.flush()


def request_pause(session: Session, experiment: Experiment) -> None:
    experiment.pause_requested = True
    session.flush()


def resume(session: Session, experiment: Experiment) -> None:
    experiment.pause_requested = False
    if experiment.state == "PAUSED":
        transition(session, experiment, "QUEUED", reason="resumed", actor="api")
        session.add(BackgroundJob(job_type="run_experiment", subject_id=experiment.id,
                                  payload_json={"experiment_id": experiment.id}))
    session.flush()


def get_runs(session: Session, experiment_id: str) -> list[InterviewRun]:
    return list(session.execute(select(InterviewRun).where(
        InterviewRun.experiment_id == experiment_id)).scalars())


def architecture_label_map(session: Session, experiment_id: str) -> dict:
    arms = session.execute(select(ExperimentArchitecture).where(
        ExperimentArchitecture.experiment_id == experiment_id)).scalars().all()
    out = {}
    for arm in arms:
        version = session.get(ArchitectureVersion, arm.architecture_version_id)
        arch = session.get(Architecture, version.architecture_id) if version else None
        out[arm.arm] = {
            "architecture_version_id": arm.architecture_version_id,
            "blind_label": arm.blind_label,
            "name": arch.name if arch else "?",
            "version": version.version if version else None}
    return out
