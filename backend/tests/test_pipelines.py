"""Tests for multi-stage pipelines, web research, and the tournament."""
import pytest


def test_pipeline_validation_limits():
    from services.pipelines import (PipelineSpec, PipelineStage,
                                    PipelineValidationError, validate_pipeline)
    with pytest.raises(PipelineValidationError):
        validate_pipeline(PipelineSpec(
            name="bad", pre_stages=[PipelineStage(stage_type="hack_the_db")]))
    with pytest.raises(PipelineValidationError):
        validate_pipeline(PipelineSpec(
            name="too many",
            pre_stages=[PipelineStage(stage_type="role_brief")] * 4))
    ok = validate_pipeline(PipelineSpec(
        name="ok", pre_stages=[PipelineStage(stage_type="company_research")],
        per_turn_stages=[PipelineStage(stage_type="answer_analyzer")]))
    assert ok.name == "ok"


def test_pipeline_mutation_bounded():
    from services.pipelines import (PipelineMutationSchema, PipelineSpec,
                                    PipelineStage, apply_pipeline_mutation)
    spec = PipelineSpec(name="x", pre_stages=[PipelineStage(stage_type="role_brief")])
    mutated = apply_pipeline_mutation(spec, PipelineMutationSchema(
        action="add_stage", section="per_turn_stages",
        new_stage=PipelineStage(stage_type="question_strategist"), reason="t"))
    assert len(mutated.per_turn_stages) == 1
    removed = apply_pipeline_mutation(mutated, PipelineMutationSchema(
        action="remove_stage", section="pre_stages", stage_index=0, reason="t"))
    assert not removed.pre_stages


def test_web_research_stub_is_labeled_and_budgeted(seeded, project_id):
    from db import session_scope
    from services import budgets, web_research
    with session_scope() as session:
        before = budgets.get_budget(session, project_id).spent_usd
        result = web_research.research(session, project_id,
                                       "backend engineer interviews", "test")
        assert result["mode"] == "stub"
        assert "STUB WEB RESEARCH" in result["raw"]
        assert "<BEGIN_CANDIDATE_DATA>" in result["wrapped"]
        after = budgets.get_budget(session, project_id).spent_usd
        assert after > before  # web research is budget-gated too


def test_pipeline_interview_runs_stages(seeded, project_id):
    """A pipeline architecture actually executes its stages as separate calls."""
    from sqlalchemy import select
    from db import session_scope
    from models import (Architecture, ArchitectureVersion,
                        CandidatePersonaVersion, InterviewRun, ModelCall)
    from services import architectures as arch_service
    from services.pipelines import run_interview_dispatch
    with session_scope() as session:
        base_arch = session.execute(select(Architecture).where(
            Architecture.code == "A1")).scalars().first()
        base = session.execute(select(ArchitectureVersion).where(
            ArchitectureVersion.architecture_id == base_arch.id)).scalars().first()
        arch = arch_service.create_architecture(session, name="[pipeline] test-p",
                                                creation_source="agent")
        components = dict(base.components_json)
        components["pipeline_stages"] = {
            "name": "test", "description": "",
            "pre_stages": [{"stage_type": "company_research", "instructions": ""},
                           {"stage_type": "role_brief", "instructions": ""}],
            "per_turn_stages": [{"stage_type": "answer_analyzer", "instructions": ""}]}
        version = arch_service.create_version(
            session, arch, components=components,
            model_config=dict(base.model_config_json),
            limits={"max_turns": 2, "run_cost_cap_usd": 2.0}, status="active")
        pv = session.execute(select(CandidatePersonaVersion)).scalars().first()
        run = InterviewRun(architecture_version_id=version.id,
                           persona_version_id=pv.id, interview_mode="HR",
                           provider_seed=5)
        session.add(run)
        session.flush()
        run_interview_dispatch(session, run, project_id)
        assert run.status == "completed"
        roles = {c.agent_role for c in session.execute(select(ModelCall).where(
            ModelCall.interview_run_id == run.id)).scalars()}
        assert "pipeline_role_brief" in roles          # pre stage ran as own call
        assert "pipeline_answer_analyzer" in roles     # per-turn stage ran
        assert run.state_json.get("pipeline_stage_log")


def test_tournament_stub_end_to_end(seeded, project_id):
    from db import session_scope
    from models import ResearchRun
    from services.tournament import run_pipeline_tournament
    with session_scope() as session:
        rr = ResearchRun(project_id=project_id, max_rounds=0, max_budget_usd=4.0,
                         log_json=[{"kind": "pipeline_tournament",
                                    "num_pipelines": 2, "improve_rounds": 0,
                                    "interview_mode": "HR", "allow_web": False}])
        session.add(rr)
        session.flush()
        rr_id = rr.id
    with session_scope() as session:
        run_pipeline_tournament(session, rr_id)
    with session_scope() as session:
        rr = session.get(ResearchRun, rr_id)
        assert rr.status == "completed"
        phases = [e.get("phase") for e in rr.log_json if isinstance(e, dict)]
        assert "result" in phases
        result = next(e for e in rr.log_json
                      if isinstance(e, dict) and e.get("phase") == "result")
        assert "not a global optimum" in result["note"]
