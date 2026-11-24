"""Final validation and final recommendation report generation.

The final report never claims a globally optimal prompt — only the
best-supported architecture within the tested search space, scenarios, models,
rubrics, and budget. Never auto-deploys.
"""
from __future__ import annotations

import statistics as pystats

from sqlalchemy import select
from sqlalchemy.orm import Session

from db import utcnow
from models import (Architecture, ArchitectureVersion, CriticReview, Experiment,
                    FinalRecommendation, HardConstraintFinding, Hypothesis,
                    InterviewRun, MetricScore, PairwiseComparison,
                    ResearchProject, ResearchReport, ScenarioSuite,
                    TimelineEvent)
from services import budgets
from services.statistics_service import pareto_frontier


def unlock_holdout_for_validation(session: Session, project_id: str) -> None:
    """Phase 5: freeze finalists, unlock holdout runtime (budget stays reserved
    for validation use only — now spendable)."""
    suite = session.execute(select(ScenarioSuite).where(
        ScenarioSuite.key == "holdout")).scalars().first()
    if suite:
        suite.locked = False
        suite.holdout_unlocked_at = utcnow()
    budgets.unlock_holdout(session, project_id)
    session.add(TimelineEvent(project_id=project_id, event_type="holdout_unlocked",
                              title="Holdout suite unlocked for final validation",
                              detail_json={}, actor="human"))
    session.flush()


def run_final_validation(session: Session, project_id: str) -> None:
    """Job: run the strongest finalist against the production baseline on the
    holdout suite (E12 semantics) — delegated to the experiment pipeline via a
    generated experiment. Kept minimal: unlock + timeline marker; the actual
    E12 experiment is launched from the UI/API with holdout=true."""
    unlock_holdout_for_validation(session, project_id)


def _architecture_quality_cost(session: Session, project_id: str) -> list[dict]:
    points = []
    version_rows = list(session.execute(select(ArchitectureVersion)).scalars())
    for version in version_rows:
        runs = list(session.execute(select(InterviewRun).where(
            InterviewRun.architecture_version_id == version.id,
            InterviewRun.status == "completed")).scalars())
        if not runs:
            continue
        run_ids = [r.id for r in runs]
        scores = [s.score for s in session.execute(select(MetricScore).where(
            MetricScore.interview_run_id.in_(run_ids))).scalars()]
        hard = len(list(session.execute(select(HardConstraintFinding).where(
            HardConstraintFinding.interview_run_id.in_(run_ids),
            HardConstraintFinding.resolved == False)).scalars()))  # noqa: E712
        arch = session.get(Architecture, version.architecture_id)
        if scores:
            points.append({
                "architecture_version_id": version.id,
                "name": f"{arch.name} v{version.version}" if arch else version.id,
                "code": arch.code if arch else "",
                "status": version.status,
                "quality": round(pystats.fmean(scores), 2),
                "quality_std": round(pystats.stdev(scores), 2) if len(scores) > 1 else 0.0,
                "cost": round(pystats.fmean([r.total_cost_usd for r in runs]), 5),
                "runs": len(runs),
                "unresolved_hard_constraints": hard,
            })
    return points


def generate_final_recommendation(session: Session, project_id: str) -> FinalRecommendation:
    project = session.get(ResearchProject, project_id)
    experiments = list(session.execute(select(Experiment).where(
        Experiment.project_id == project_id,
        Experiment.is_template == False)).scalars())  # noqa: E712
    hypotheses = list(session.execute(select(Hypothesis).where(
        Hypothesis.project_id == project_id)).scalars())
    points = _architecture_quality_cost(session, project_id)
    frontier = pareto_frontier(points) if points else []
    eligible = [p for p in points if p["unresolved_hard_constraints"] == 0
                and not p.get("dominated")]
    budget = budgets.get_budget(session, project_id)

    holdout = session.execute(select(ScenarioSuite).where(
        ScenarioSuite.key == "holdout")).scalars().first()
    holdout_validated = bool(holdout and holdout.holdout_unlocked_at)
    completed = [e for e in experiments if e.state == "COMPLETED"]
    confirmed = [e for e in completed
                 if e.result_label in ("confirmed_within_suite",)]

    if not points:
        status = "inconclusive"
        best = None
    elif any(p["unresolved_hard_constraints"] > 0 for p in points) and not eligible:
        status = "safety_rejected"
        best = None
    elif eligible and holdout_validated and confirmed:
        status = "recommended_for_human_validation"
        best = max(eligible, key=lambda p: p["quality"])
    elif eligible:
        status = "promising_but_unconfirmed"
        best = max(eligible, key=lambda p: p["quality"])
    else:
        status = "inconclusive"
        best = None

    critics = list(session.execute(select(CriticReview)).scalars())
    pairwise = list(session.execute(select(PairwiseComparison)).scalars())

    report = {
        "scope": {
            "note": ("This report identifies the best-supported architecture WITHIN the "
                     "tested search space, scenarios, models, rubrics and budget. It is "
                     "NOT a claim of a globally optimal prompt."),
            "project": project.name if project else project_id,
            "experiments_run": len(experiments),
            "experiments_completed": len(completed),
            "hypotheses_tested": len(hypotheses),
            "holdout_validated": holdout_validated,
        },
        "baseline": "evior-production-baseline-v1 (A0, protected, immutable)",
        "recommended_overall": best,
        "recommended_by_mode": {},  # populated below when per-mode evidence exists
        "pareto_frontier": frontier,
        "quantitative_findings": [
            {"experiment": e.name, "code": e.code, "label": e.result_label,
             "cost_usd": e.actual_cost_usd,
             "comparison_overall": (e.decision_json or {}).get("comparison", {}).get("overall")}
            for e in completed],
        "pairwise_findings": [
            {"experiment_id": p.experiment_id, "ordering": p.ordering,
             "overall_winner": p.judge_output_json.get("overall_winner"),
             "confidence": p.judge_output_json.get("confidence")}
            for p in pairwise],
        "critic_reviews": [c.output_json for c in critics],
        "hard_constraint_summary": {
            p["name"]: p["unresolved_hard_constraints"] for p in points},
        "costs": {"total_spent_usd": budget.spent_usd,
                  "total_budget_usd": budget.total_usd,
                  "holdout_reserve_usd": budget.holdout_reserve_usd},
        "failed_approaches": [
            {"experiment": e.name, "label": e.result_label}
            for e in experiments if e.result_label in ("rejected", "safety_rejected")],
        "limitations": [
            "16 synthetic personas — real-candidate behavior may differ.",
            "Small repetition counts; labels are exploratory research statements.",
            "Stub-mode runs (if any) are labeled and excluded from real findings.",
            "Evaluator LLMs share family biases with the interviewer models.",
            "Deterministic metrics are labeled heuristics, not ground truth."],
        "production_safeguards": [
            "Human review required before any production adoption.",
            "Never auto-deploy: this lab has no production write path.",
            "Recommendations are decision support, not hiring decisions.",
            "Run fairness evaluation on real traffic samples before rollout."],
        "next_experiments": [
            "Ablation of any bundled interventions that were promoted.",
            "Cross-role-family regression at higher repetition counts.",
            "Human calibration of evaluator scores on a sample of transcripts."],
    }

    # per-mode recommendation when evidence exists
    mode_scores: dict[str, dict[str, list[float]]] = {}
    for run in session.execute(select(InterviewRun).where(
            InterviewRun.status == "completed")).scalars():
        scores = [s.score for s in session.execute(select(MetricScore).where(
            MetricScore.interview_run_id == run.id)).scalars()]
        if scores:
            mode_scores.setdefault(run.interview_mode, {}).setdefault(
                run.architecture_version_id, []).extend(scores)
    for mode, per_arch in mode_scores.items():
        best_arch = max(per_arch.items(), key=lambda kv: pystats.fmean(kv[1]))
        report["recommended_by_mode"][mode] = {
            "architecture_version_id": best_arch[0],
            "mean_score": round(pystats.fmean(best_arch[1]), 2),
            "n_scores": len(best_arch[1])}

    recommendation = FinalRecommendation(project_id=project_id, status=status,
                                         report_json=report)
    session.add(recommendation)
    session.add(ResearchReport(project_id=project_id, report_type="final",
                               content_json=report))
    session.add(TimelineEvent(project_id=project_id, event_type="final_recommendation",
                              title=f"Final recommendation generated: {status}",
                              detail_json={"status": status}, actor="system"))
    session.flush()
    return recommendation
