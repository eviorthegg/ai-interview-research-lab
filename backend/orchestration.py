"""Experiment pipeline execution + background job queue + worker loop.

Worker claims jobs atomically, heartbeats, recovers abandoned jobs, checks
cancellation before/after every agent call and before each interview turn,
and uses idempotent step keys so retries never duplicate cost.
"""
from __future__ import annotations

import json
import random
import statistics as pystats
import time
import traceback
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from db import session_scope, utcnow
from models import (BackgroundJob, CriticReview, Experiment,
                    ExperimentArchitecture, ExperimentScenario,
                    HardConstraintFinding, HumanReviewItem, InterviewRun,
                    JudgeDisagreement, MetricScore, TimelineEvent)
from schemas import ExperimentDesignSchema
from services import budgets
from services.deterministic_metrics import analyze_run
from services.evaluation import (MODE_METRIC_GROUPS, UNIVERSAL_METRICS,
                                 detect_disagreements, position_bias_detected,
                                 run_evaluator, run_pairwise)
from services.experiments import DEFAULT_PROMOTION_CRITERIA
from services.interview_runtime import InterviewCancelled
from services.pipelines import run_interview_dispatch
from services.provider import (CallContext, CancellationRequested,
                               SchemaValidationFailed, call_llm)
from services.statistics_service import compare_arms
from state_machine import emit_progress, transition
from config import get_config


# Pairwise judging costs two LLM calls per pair (both orderings), so only a
# sample of pairs is judged.
MAX_PAIRWISE_JUDGED = 6


def select_pairwise_sample(by_key: dict[tuple, dict], cap: int, seed: str) -> list[dict]:
    """Pick up to `cap` complete control/variant pairs to judge.

    The sample is random, so every scenario has the same chance of being judged
    instead of always the first ones in insertion order. It is seeded with the
    experiment id, so re-running the same experiment judges the same pairs.
    """
    complete = [by_key[k] for k in sorted(by_key, key=repr)
                if "control" in by_key[k] and "variant" in by_key[k]]
    if len(complete) <= cap:
        return complete
    return random.Random(seed).sample(complete, cap)


class ExperimentAborted(Exception):
    def __init__(self, terminal_state: str, reason: str):
        super().__init__(reason)
        self.terminal_state = terminal_state
        self.reason = reason


def _cancellation_check_factory(session: Session, experiment_id: str):
    def check():
        session.expire_all()
        exp = session.get(Experiment, experiment_id)
        if exp and exp.cancel_requested:
            raise CancellationRequested(f"Experiment {experiment_id} cancellation requested.")
        if exp and exp.pause_requested:
            raise ExperimentAborted("PAUSED", "pause requested")
    return check


def run_experiment_pipeline(session: Session, experiment: Experiment) -> None:
    """Full pipeline: PREPARING → ... → COMPLETED (or terminal alternative)."""
    design = ExperimentDesignSchema.model_validate(experiment.spec_json)
    project_id = experiment.project_id
    check_cancel = _cancellation_check_factory(session, experiment.id)
    is_holdout = design.holdout

    arms = list(session.execute(select(ExperimentArchitecture).where(
        ExperimentArchitecture.experiment_id == experiment.id)).scalars())
    scenarios = list(session.execute(select(ExperimentScenario).where(
        ExperimentScenario.experiment_id == experiment.id)).scalars())

    def spent() -> float:
        session.expire_all()
        exp = session.get(Experiment, experiment.id)
        return exp.actual_cost_usd if exp else 0.0

    if experiment.cancel_requested or experiment.state == "CANCEL_REQUESTED":
        # Cancelled before the worker started: no paid call may happen.
        if experiment.state != "CANCEL_REQUESTED":
            transition(session, experiment, "CANCEL_REQUESTED",
                       reason="cancel before start", actor="worker")
        transition(session, experiment, "CANCELLED",
                   reason="cancelled before execution", actor="worker")
        session.commit()
        return

    try:
        transition(session, experiment, "PREPARING", actor="worker")
        session.commit()

        # -------- interviews
        transition(session, experiment, "RUNNING_INTERVIEWS", actor="worker")
        session.commit()
        runs: list[InterviewRun] = []
        for scenario in scenarios:
            for arm in arms:
                for rep in range(scenario.repetitions):
                    check_cancel()  # before starting the next run
                    step_key = (f"run:{experiment.id}:{arm.arm}:"
                                f"{scenario.persona_version_id}:{scenario.interview_mode}:{rep}")
                    existing = session.execute(select(InterviewRun).where(
                        InterviewRun.experiment_id == experiment.id,
                        InterviewRun.architecture_version_id == arm.architecture_version_id,
                        InterviewRun.persona_version_id == scenario.persona_version_id,
                        InterviewRun.interview_mode == scenario.interview_mode,
                        InterviewRun.repetition_index == rep)).scalars().first()
                    if existing and existing.status == "completed":
                        runs.append(existing)  # idempotent step: never re-charge
                        continue
                    run = existing or InterviewRun(
                        experiment_id=experiment.id,
                        architecture_version_id=arm.architecture_version_id,
                        persona_version_id=scenario.persona_version_id,
                        interview_mode=scenario.interview_mode,
                        repetition_index=rep,
                        provider_seed=hash(step_key) % (2**31))
                    if not existing:
                        session.add(run)
                        session.flush()
                    emit_progress(session, "experiment", experiment.id, "interview_started",
                                  {"run_id": run.id, "arm": arm.arm,
                                   "persona_version_id": scenario.persona_version_id,
                                   "mode": scenario.interview_mode, "repetition": rep})
                    session.commit()
                    try:
                        run_interview_dispatch(
                            session, run, project_id,
                            experiment_cap_usd=design.max_cost_usd,
                            experiment_spent_fn=spent,
                            cancellation_check=check_cancel, is_holdout=is_holdout)
                    except budgets.BudgetExceededError as exc:
                        run.status = "budget_blocked"
                        run.termination_reason = "cost_limit"
                        session.commit()
                        raise ExperimentAborted("BUDGET_BLOCKED", str(exc))
                    except SchemaValidationFailed as exc:
                        run.status = "failed"
                        run.termination_reason = "schema_validation_failed"
                        session.commit()
                        raise ExperimentAborted("FAILED", str(exc))
                    runs.append(run)
                    emit_progress(session, "experiment", experiment.id,
                                  "interview_completed",
                                  {"run_id": run.id, "cost_usd": run.total_cost_usd})
                    session.commit()

        # -------- deterministic analysis
        transition(session, experiment, "RUNNING_DETERMINISTIC_ANALYSIS", actor="worker")
        session.commit()
        for run in runs:
            check_cancel()
            analyze_run(session, run.id)
            session.commit()

        # -------- evaluators
        transition(session, experiment, "RUNNING_EVALUATORS", actor="worker")
        session.commit()
        eval_model = design.model_config_lock.get("evaluator_model",
                                                  get_config().evaluator_model)
        for run in runs:
            check_cancel()  # before evaluation
            mode_metrics = MODE_METRIC_GROUPS.get(run.interview_mode, [])
            run_evaluator(session, run, project_id, "core_qa", UNIVERSAL_METRICS,
                          design.rubric_version_id, eval_model,
                          cancellation_check=check_cancel)
            if mode_metrics:
                run_evaluator(session, run, project_id, "mode_specialist", mode_metrics,
                              design.rubric_version_id, eval_model,
                              cancellation_check=check_cancel)
            # fairness evaluator only when deterministic checks warned (or holdout)
            warnings = session.execute(select(HardConstraintFinding).where(
                HardConstraintFinding.interview_run_id == run.id)).scalars().first()
            if warnings or is_holdout:
                run_evaluator(session, run, project_id, "fairness",
                              ["fairness", "privacy", "job_relevance"],
                              design.rubric_version_id, eval_model,
                              cancellation_check=check_cancel)
            detect_disagreements(session, run.id, experiment.id)
            session.commit()

        # -------- pairwise (both orderings; paired by scenario/mode/rep)
        control_arm = next(a for a in arms if a.arm == "control")
        variant_arm = next(a for a in arms if a.arm == "variant")
        by_key: dict[tuple, dict[str, InterviewRun]] = {}
        for run in runs:
            if run.status != "completed":
                continue
            key = (run.persona_version_id, run.interview_mode, run.repetition_index)
            arm_name = ("control" if run.architecture_version_id
                        == control_arm.architecture_version_id else "variant")
            by_key.setdefault(key, {})[arm_name] = run
        position_bias_count = 0
        complete_pairs = sum(1 for p in by_key.values() if "control" in p and "variant" in p)
        judged_pairs = select_pairwise_sample(by_key, MAX_PAIRWISE_JUDGED, seed=experiment.id)
        for pair in judged_pairs:
            check_cancel()
            comparisons = run_pairwise(session, experiment.id, project_id,
                                       pair["control"], pair["variant"], eval_model,
                                       cancellation_check=check_cancel)
            if position_bias_detected(comparisons):
                position_bias_count += 1
            session.commit()

        # -------- aggregation
        transition(session, experiment, "AGGREGATING", actor="worker")
        session.commit()
        comparison = _aggregate(session, experiment, design, control_arm, variant_arm, runs)
        comparison["position_bias_pairs"] = position_bias_count
        comparison["pairwise_sample"] = {"judged": len(judged_pairs),
                                         "available": complete_pairs}
        experiment.decision_json = {"comparison": comparison}
        session.commit()

        # -------- critic (promoted candidates and disputed outcomes)
        transition(session, experiment, "CRITIC_REVIEW", actor="worker")
        session.commit()
        label = comparison["overall"]["result_label"]
        open_disagreements = list(session.execute(select(JudgeDisagreement).where(
            JudgeDisagreement.experiment_id == experiment.id,
            JudgeDisagreement.status == "open")).scalars())
        if label in ("promising", "confirmed_within_suite") or open_disagreements:
            check_cancel()
            critic = _run_critic(session, experiment, comparison, eval_model, check_cancel)
            experiment.decision_json = {**experiment.decision_json,
                                        "critic": critic}
            session.commit()

        # -------- decide
        transition(session, experiment, "DECIDING", actor="worker")
        session.commit()
        decision = _decide(session, experiment, design, comparison, open_disagreements)
        experiment.decision_json = {**experiment.decision_json, "decision": decision}
        experiment.result_label = decision["result_label"]
        if decision["needs_human_review"]:
            transition(session, experiment, "NEEDS_HUMAN_REVIEW",
                       reason=decision["reason"], actor="worker")
            session.add(HumanReviewItem(
                project_id=project_id, subject_type="experiment",
                subject_id=experiment.id, reason=decision["reason"]))
        else:
            transition(session, experiment, "COMPLETED",
                       reason=decision["reason"], actor="worker")
        session.add(TimelineEvent(
            project_id=project_id, event_type="experiment_decided",
            title=f"{experiment.name}: {decision['result_label']}",
            detail_json=decision, actor="worker"))
        session.commit()

    except ExperimentAborted as aborted:
        transition(session, experiment, aborted.terminal_state,
                   reason=aborted.reason, actor="worker")
        session.commit()
    except CancellationRequested:
        for state in ("CANCEL_REQUESTED", "CANCELLED"):
            try:
                transition(session, experiment, state, reason="user cancel", actor="worker")
            except Exception:
                pass
        session.commit()
    except InterviewCancelled:
        try:
            transition(session, experiment, "CANCELLED", reason="user cancel", actor="worker")
        except Exception:
            pass
        session.commit()


def _aggregate(session: Session, experiment: Experiment,
               design: ExperimentDesignSchema, control_arm, variant_arm,
               runs: list[InterviewRun]) -> dict:
    def scores_for(version_id: str) -> dict[str, dict[str, float]]:
        out: dict[str, dict[str, list[float]]] = {}
        for run in runs:
            if run.architecture_version_id != version_id or run.status != "completed":
                continue
            scenario_key = f"{run.persona_version_id}:{run.interview_mode}"
            for score in session.execute(select(MetricScore).where(
                    MetricScore.interview_run_id == run.id)).scalars():
                out.setdefault(score.metric_key, {}).setdefault(scenario_key, []).append(score.score)
        return {m: {k: pystats.fmean(v) for k, v in per.items()}
                for m, per in out.items()}

    control_scores = scores_for(control_arm.architecture_version_id)
    variant_scores = scores_for(variant_arm.architecture_version_id)

    def hard_constraints_for(version_id: str) -> int:
        run_ids = [r.id for r in runs if r.architecture_version_id == version_id]
        if not run_ids:
            return 0
        return len(list(session.execute(select(HardConstraintFinding).where(
            HardConstraintFinding.interview_run_id.in_(run_ids),
            HardConstraintFinding.resolved == False)).scalars()))  # noqa: E712

    hc_control = hard_constraints_for(control_arm.architecture_version_id)
    hc_variant = hard_constraints_for(variant_arm.architecture_version_id)
    criteria = {**DEFAULT_PROMOTION_CRITERIA, **(design.promotion_criteria or {})}
    result = compare_arms(
        control_scores, variant_scores,
        min_effect_points=float(criteria["min_quality_improvement_points"]),
        hard_constraint_regression=(hc_variant > hc_control),
        repetitions=design.repetitions)

    def cost_for(version_id: str) -> dict:
        vals = [r.total_cost_usd for r in runs
                if r.architecture_version_id == version_id and r.status == "completed"]
        lat = [r.total_latency_ms for r in runs
               if r.architecture_version_id == version_id and r.status == "completed"]
        return {"mean_cost_usd": round(pystats.fmean(vals), 5) if vals else None,
                "mean_latency_ms": round(pystats.fmean(lat), 1) if lat else None}

    result["efficiency"] = {"control": cost_for(control_arm.architecture_version_id),
                            "variant": cost_for(variant_arm.architecture_version_id)}
    result["hard_constraints"] = {"control": hc_control, "variant": hc_variant}
    c_cost = result["efficiency"]["control"]["mean_cost_usd"]
    v_cost = result["efficiency"]["variant"]["mean_cost_usd"]
    if c_cost and v_cost and c_cost > 0:
        result["efficiency"]["cost_increase_ratio"] = round((v_cost - c_cost) / c_cost, 3)
    return result


def _run_critic(session: Session, experiment: Experiment, comparison: dict,
                model: str, check_cancel) -> dict:
    from schemas import CriticReviewSchema
    messages = [
        {"role": "system", "content":
            "SYSTEM INSTRUCTIONS\nYou are a research critic. Challenge causal claims, "
            "check for evaluator leakage, weak controls, synthetic-persona overfitting, "
            "and metrics moving in opposite directions. Output JSON matching the "
            "critic_review schema:\n"
            "{\n"
            "  \"causal_claim_issues\": [str],\n"
            "  \"leakage_concerns\": [str],\n"
            "  \"control_weaknesses\": [str],\n"
            "  \"overfitting_risks\": [str],\n"
            "  \"conflicting_metrics\": [str],\n"
            "  \"verdict\": \"upheld\"|\"challenged\"|\"rejected\",\n"
            "  \"rationale\": str\n"
            "}"},
        {"role": "user", "content":
            "EXPERIMENT COMPARISON RESULT (aggregated, blind labels):\n"
            + json.dumps({k: v for k, v in comparison.items()
                          if k in ("per_metric", "overall", "hard_constraints")},
                         default=str)[:8000]}]
    ctx = CallContext(session=session, project_id=experiment.project_id,
                      agent_role="critic", experiment_id=experiment.id,
                      cancellation_check=check_cancel)
    result = call_llm(ctx, messages, model, schema=CriticReviewSchema,
                      handoff_to="research_director")
    review = result.parsed.model_dump()
    session.add(CriticReview(experiment_id=experiment.id, output_json=review,
                             cost_usd=result.cost_usd))
    session.flush()
    return review


def _decide(session: Session, experiment: Experiment, design: ExperimentDesignSchema,
            comparison: dict, open_disagreements: list) -> dict:
    label = comparison["overall"]["result_label"]
    criteria = {**DEFAULT_PROMOTION_CRITERIA, **(design.promotion_criteria or {})}
    reasons = []
    needs_review = False
    if comparison["hard_constraints"]["variant"] > comparison["hard_constraints"]["control"]:
        label = "safety_rejected"
        reasons.append("Variant introduced new unresolved hard-constraint findings.")
    ratio = comparison.get("efficiency", {}).get("cost_increase_ratio")
    if ratio is not None and ratio > criteria["max_cost_increase_ratio"] \
            and label in ("promising", "confirmed_within_suite"):
        label = "inconclusive"
        reasons.append(f"Cost increase {ratio:.0%} exceeds configured tolerance "
                       f"{criteria['max_cost_increase_ratio']:.0%}.")
    if open_disagreements:
        needs_review = True
        reasons.append(f"{len(open_disagreements)} unresolved evaluator disagreements "
                       "— routed to human review (never silently averaged).")
    if comparison["overall"].get("single_scenario_driver_warning"):
        reasons.append("Improvement may be driven by a single scenario — flagged.")
    critic = (experiment.decision_json or {}).get("critic")
    if critic and critic.get("verdict") == "rejected":
        label = "inconclusive"
        needs_review = True
        reasons.append("Critic rejected the causal claim.")
    return {"result_label": label, "needs_human_review": needs_review,
            "reason": "; ".join(reasons) or "Criteria evaluated automatically.",
            "criteria_used": criteria}


# ------------------------------------------------------------------- worker

def claim_next_job(session: Session, worker_name: str) -> BackgroundJob | None:
    """Atomic claim via optimistic UPDATE."""
    now = utcnow()
    abandoned_cutoff = now - timedelta(seconds=get_config().job_abandon_timeout_seconds)
    # recover abandoned jobs
    session.execute(update(BackgroundJob)
                    .where(BackgroundJob.status.in_(("claimed", "running")),
                           BackgroundJob.heartbeat_at < abandoned_cutoff)
                    .values(status="queued", claimed_by=None, error="recovered: heartbeat timeout"))
    session.commit()
    job = session.execute(select(BackgroundJob)
                          .where(BackgroundJob.status == "queued")
                          .order_by(BackgroundJob.created_at)).scalars().first()
    if job is None:
        return None
    result = session.execute(
        update(BackgroundJob)
        .where(BackgroundJob.id == job.id, BackgroundJob.status == "queued")
        .values(status="claimed", claimed_by=worker_name, claimed_at=now,
                heartbeat_at=now, attempts=BackgroundJob.attempts + 1))
    session.commit()
    if result.rowcount != 1:
        return None
    session.refresh(job)
    return job


def heartbeat(session: Session, job: BackgroundJob) -> None:
    session.execute(update(BackgroundJob).where(BackgroundJob.id == job.id)
                    .values(heartbeat_at=utcnow()))
    session.commit()


def process_job(session: Session, job: BackgroundJob) -> None:
    job.status = "running"
    job.heartbeat_at = utcnow()
    session.commit()
    try:
        if job.job_type == "run_experiment":
            experiment = session.get(Experiment, job.payload_json["experiment_id"])
            if experiment is None:
                raise ValueError("experiment not found")
            run_experiment_pipeline(session, experiment)
        elif job.job_type == "autonomous_cycle":
            from services.autonomous import run_autonomous_cycle
            run_autonomous_cycle(session, job.payload_json["research_run_id"])
        elif job.job_type == "pipeline_tournament":
            from services.tournament import run_pipeline_tournament
            run_pipeline_tournament(session, job.payload_json["research_run_id"])
        elif job.job_type == "final_validation":
            from services.reporting import run_final_validation
            run_final_validation(session, job.payload_json["project_id"])
        job.status = "completed"
        session.commit()
    except Exception as exc:
        session.rollback()
        job = session.get(BackgroundJob, job.id)
        job.status = "failed"
        raw_error = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()[-2000:]}"
        # Sanitize Unicode chars (e.g. → in experiment names) for Windows cp1252 safety
        job.error = raw_error.encode("utf-8", errors="replace").decode("utf-8")
        session.commit()
        raise


def worker_loop(poll_seconds: float = 1.0, worker_name: str = "worker-1",
                max_iterations: int | None = None) -> None:
    """Main worker loop (separate process). max_iterations for tests."""
    iterations = 0
    while max_iterations is None or iterations < max_iterations:
        iterations += 1
        with session_scope() as session:
            job = claim_next_job(session, worker_name)
        if job is None:
            time.sleep(poll_seconds)
            continue
        with session_scope() as session:
            job = session.get(BackgroundJob, job.id)
            try:
                process_job(session, job)
            except Exception:
                pass  # recorded on the job row; loop continues
