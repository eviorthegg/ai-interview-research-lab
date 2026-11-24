"""Integration tests: experiment creation → estimate → launch → worker →
evaluation → aggregation → decision; cancellation; idempotent launch;
reproducibility."""
import pytest


def _make_experiment(session, project_id, personas=1, reps=1):
    from sqlalchemy import select
    from models import Experiment
    from schemas import ExperimentDesignSchema
    from services.experiments import create_experiment
    template = session.execute(select(Experiment).where(
        Experiment.code == "E1")).scalars().first()
    design = ExperimentDesignSchema.model_validate(template.spec_json)
    design.scenario_persona_version_ids = design.scenario_persona_version_ids[:personas]
    design.interview_modes = ["HR"]
    design.repetitions = reps
    return create_experiment(session, project_id, "integration run", design)


def test_full_pipeline_stub(seeded, project_id):
    from sqlalchemy import select
    from db import session_scope
    from models import (Experiment, InterviewRun, MetricScore,
                        DeterministicMeasurement, PairwiseComparison)
    from orchestration import worker_loop
    from services.experiments import launch
    with session_scope() as session:
        exp = _make_experiment(session, project_id)
        exp_id = exp.id
    with session_scope() as session:
        exp = session.get(Experiment, exp_id)
        result = launch(session, exp)
        assert result["state"] == "QUEUED"
        assert result["estimate"]["estimated_total_usd"] > 0
    worker_loop(poll_seconds=0.05, max_iterations=4)
    with session_scope() as session:
        exp = session.get(Experiment, exp_id)
        assert exp.state in ("COMPLETED", "NEEDS_HUMAN_REVIEW")
        assert exp.result_label is not None
        assert exp.actual_cost_usd > 0
        runs = session.execute(select(InterviewRun).where(
            InterviewRun.experiment_id == exp_id)).scalars().all()
        assert len(runs) == 2  # control + variant
        assert all(r.status == "completed" for r in runs)
        assert session.execute(select(MetricScore)).scalars().first() is not None
        assert session.execute(select(DeterministicMeasurement)).scalars().first() is not None
        assert session.execute(select(PairwiseComparison)).scalars().first() is not None
        comparison = exp.decision_json["comparison"]
        assert "overall" in comparison and "bootstrap_ci" in comparison["overall"]


def test_idempotent_launch(seeded, project_id):
    from db import session_scope
    from models import BackgroundJob, Experiment
    from sqlalchemy import select
    from services.experiments import launch
    with session_scope() as session:
        exp = _make_experiment(session, project_id)
        exp_id = exp.id
    with session_scope() as session:
        exp = session.get(Experiment, exp_id)
        r1 = launch(session, exp, idempotency_key="same-key")
    with session_scope() as session:
        exp = session.get(Experiment, exp_id)
        r2 = launch(session, exp, idempotency_key="same-key")
        assert r1["job_id"] == r2["job_id"]
        jobs = session.execute(select(BackgroundJob).where(
            BackgroundJob.subject_id == exp_id)).scalars().all()
        assert len(jobs) == 1


def test_cancellation_stops_before_next_paid_call(seeded, project_id):
    from db import session_scope
    from models import Experiment, ModelCall
    from sqlalchemy import select
    from services.experiments import launch, request_cancel
    from orchestration import run_experiment_pipeline
    with session_scope() as session:
        exp = _make_experiment(session, project_id)
        exp_id = exp.id
    with session_scope() as session:
        exp = session.get(Experiment, exp_id)
        launch(session, exp)
        request_cancel(session, exp)  # cancel BEFORE worker runs
    with session_scope() as session:
        exp = session.get(Experiment, exp_id)
        run_experiment_pipeline(session, exp)
    with session_scope() as session:
        exp = session.get(Experiment, exp_id)
        assert exp.state == "CANCELLED"
        calls = session.execute(select(ModelCall).where(
            ModelCall.experiment_id == exp_id)).scalars().all()
        assert len(calls) == 0  # cancelled before any paid call


def test_budget_blocked_experiment(seeded, project_id):
    from db import session_scope
    from models import Experiment
    from sqlalchemy import select
    from schemas import ExperimentDesignSchema
    from services.experiments import create_experiment, launch
    from services.budgets import BudgetExceededError
    with session_scope() as session:
        template = session.execute(select(Experiment).where(
            Experiment.code == "E1")).scalars().first()
        design = ExperimentDesignSchema.model_validate(template.spec_json)
        design.scenario_persona_version_ids = design.scenario_persona_version_ids[:1]
        design.interview_modes = ["HR"]
        design.max_cost_usd = 0.0001  # far below any estimate
        exp = create_experiment(session, project_id, "too cheap", design)
        with pytest.raises(BudgetExceededError):
            launch(session, exp)
        assert exp.state == "BUDGET_BLOCKED"


def test_run_reproducibility_snapshot(seeded, project_id):
    from db import session_scope
    from models import Experiment, InterviewRun
    from sqlalchemy import select
    from orchestration import worker_loop
    from services.experiments import launch
    with session_scope() as session:
        exp = _make_experiment(session, project_id)
        exp_id = exp.id
    with session_scope() as session:
        exp = session.get(Experiment, exp_id)
        launch(session, exp)
    worker_loop(poll_seconds=0.05, max_iterations=4)
    with session_scope() as session:
        runs = session.execute(select(InterviewRun).where(
            InterviewRun.experiment_id == exp_id)).scalars().all()
        for run in runs:
            snap = run.config_snapshot_json
            assert snap["configuration_hash"]
            assert snap["components"]
            assert snap["prompt_module_versions"] is not None
            assert run.provider_seed is not None


def test_evaluator_disagreement_preserved(seeded, project_id):
    from sqlalchemy import select
    from db import session_scope
    from models import (ArchitectureVersion, CandidatePersonaVersion,
                        EvaluationRun, InterviewRun, MetricScore)
    from services.evaluation import detect_disagreements
    with session_scope() as session:
        pv = session.execute(select(CandidatePersonaVersion)).scalars().first()
        version = session.execute(select(ArchitectureVersion)).scalars().first()
        run = InterviewRun(architecture_version_id=version.id,
                           persona_version_id=pv.id, interview_mode="HR",
                           status="completed")
        session.add(run)
        session.flush()
        run_x = run.id
        e1 = EvaluationRun(interview_run_id=run_x, evaluator_role="core_qa")
        e2 = EvaluationRun(interview_run_id=run_x, evaluator_role="mode_specialist")
        session.add_all([e1, e2])
        session.flush()
        session.add(MetricScore(evaluation_run_id=e1.id, interview_run_id=run_x,
                                metric_key="follow_up_depth", score=90,
                                confidence=0.9, evidence_turn_ids_json=["turn-1"]))
        session.add(MetricScore(evaluation_run_id=e2.id, interview_run_id=run_x,
                                metric_key="follow_up_depth", score=40,
                                confidence=0.8, evidence_turn_ids_json=["turn-1"]))
        session.flush()
        found = detect_disagreements(session, run_x)
        assert any(d.metric_key == "follow_up_depth" for d in found)
        assert found[0].detail_json["gap"] == 50


def test_final_report_generation(seeded, project_id):
    from db import session_scope
    from services.reporting import generate_final_recommendation
    with session_scope() as session:
        rec = generate_final_recommendation(session, project_id)
        assert rec.status in ("inconclusive", "promising_but_unconfirmed",
                              "recommended_for_human_validation", "safety_rejected")
        assert "NOT a claim of a globally optimal prompt" in rec.report_json["scope"]["note"]


def test_autonomous_cycle_bounded(seeded, project_id):
    from db import session_scope
    from models import ResearchRun
    from services.autonomous import run_autonomous_cycle
    with session_scope() as session:
        rr = ResearchRun(project_id=project_id, max_rounds=1, max_budget_usd=2.0)
        session.add(rr)
        session.flush()
        rr_id = rr.id
    with session_scope() as session:
        run_autonomous_cycle(session, rr_id)
    with session_scope() as session:
        rr = session.get(ResearchRun, rr_id)
        assert rr.status in ("completed", "stopped_budget", "needs_human_review",
                             "stopped_safety")
        assert rr.rounds_completed <= 1
        assert rr.stop_reason
