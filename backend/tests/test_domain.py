"""Unit tests: state machine, config hashing, schema validation, mutations,
deterministic metrics, statistics, prompt rendering."""
import pytest


def test_state_machine_transitions(seeded, project_id):
    from db import session_scope
    from models import Experiment
    from state_machine import InvalidTransition, transition
    with session_scope() as session:
        exp = Experiment(project_id=project_id, name="sm", state="DRAFT")
        session.add(exp)
        session.flush()
        transition(session, exp, "VALIDATING_CONFIGURATION")
        transition(session, exp, "ESTIMATING_COST")
        with pytest.raises(InvalidTransition):
            transition(session, exp, "COMPLETED")
        transition(session, exp, "QUEUED")
        transition(session, exp, "PREPARING")
        transition(session, exp, "CANCEL_REQUESTED")
        transition(session, exp, "CANCELLED")
        with pytest.raises(InvalidTransition):
            transition(session, exp, "QUEUED")  # terminal


def test_state_events_recorded(seeded, project_id):
    from sqlalchemy import select
    from db import session_scope
    from models import Experiment, RunStateEvent
    from state_machine import transition
    with session_scope() as session:
        exp = Experiment(project_id=project_id, name="ev", state="DRAFT")
        session.add(exp)
        session.flush()
        transition(session, exp, "VALIDATING_CONFIGURATION")
        events = session.execute(select(RunStateEvent).where(
            RunStateEvent.experiment_id == exp.id)).scalars().all()
        assert len(events) == 1
        assert events[0].from_state == "DRAFT"


def test_configuration_hash_stable():
    from services.architectures import configuration_hash
    h1 = configuration_hash({"a": 1, "b": 2}, {"m": "x"}, {}, {})
    h2 = configuration_hash({"b": 2, "a": 1}, {"m": "x"}, {}, {})
    assert h1 == h2
    h3 = configuration_hash({"a": 2, "b": 2}, {"m": "x"}, {}, {})
    assert h1 != h3


def test_schema_validation_rejects_bad_evaluator_output():
    from pydantic import ValidationError
    from schemas import EvaluatorOutputSchema
    with pytest.raises(ValidationError):
        EvaluatorOutputSchema.model_validate({"scores": [{"metric_key": "x",
                                                          "score": 150}]})


def test_single_component_mutation_enforced(seeded, project_id):
    from sqlalchemy import select
    from db import session_scope
    from models import Architecture, ArchitectureVersion
    from schemas import MutationSchema
    from services import architectures as arch_service
    with session_scope() as session:
        a1 = session.execute(select(Architecture).where(
            Architecture.code == "A1")).scalars().first()
        v1 = session.execute(select(ArchitectureVersion).where(
            ArchitectureVersion.architecture_id == a1.id)).scalars().first()
        bad = MutationSchema(
            parent_architecture_id=v1.id, hypothesis_id="h",
            mutation_scope="single_component", target_component="follow_up_policy",
            proposed_after={"follow_up_policy": "evidence_gap_tracking",
                            "memory_strategy": "hybrid"},
            reason="two changes")
        with pytest.raises(arch_service.ArchitectureError):
            arch_service.apply_mutation(session, bad, project_id)
        good = MutationSchema(
            parent_architecture_id=v1.id, hypothesis_id="h",
            mutation_scope="single_component", target_component="follow_up_policy",
            proposed_after={"follow_up_policy": "evidence_gap_tracking"},
            reason="one change")
        child = arch_service.apply_mutation(session, good, project_id)
        assert child.components_json["follow_up_policy"] == "evidence_gap_tracking"
        # duplicate blocked
        with pytest.raises(arch_service.DuplicateMutationError):
            arch_service.apply_mutation(session, good, project_id)


def test_lineage(seeded):
    from sqlalchemy import select
    from db import session_scope
    from models import Architecture, ArchitectureVersion
    from services import architectures as arch_service
    with session_scope() as session:
        a1 = session.execute(select(Architecture).where(
            Architecture.code == "A1")).scalars().first()
        v1 = session.execute(select(ArchitectureVersion).where(
            ArchitectureVersion.architecture_id == a1.id)).scalars().first()
        chain = arch_service.lineage(session, v1)
        assert chain[-1]["architecture_version_id"] == v1.id


def test_deterministic_metrics(seeded, project_id):
    from sqlalchemy import select
    from db import session_scope
    from models import (ArchitectureVersion, CandidatePersonaVersion,
                        DeterministicMeasurement, InterviewRun, InterviewTurn)
    from services.deterministic_metrics import analyze_run
    with session_scope() as session:
        pv = session.execute(select(CandidatePersonaVersion)).scalars().first()
        version = session.execute(select(ArchitectureVersion)).scalars().first()
        run = InterviewRun(architecture_version_id=version.id,
                           persona_version_id=pv.id, interview_mode="HR",
                           status="completed")
        session.add(run)
        session.flush()
        session.add(InterviewTurn(interview_run_id=run.id, turn_index=0,
                                  interviewer_text="Tell me about your last project?",
                                  candidate_text="We built a billing system.",
                                  interviewer_meta_json={"topic": "ownership"}))
        session.add(InterviewTurn(interview_run_id=run.id, turn_index=1,
                                  interviewer_text="Tell me about your last project?",
                                  candidate_text="I already told you.",
                                  interviewer_meta_json={"topic": "ownership"}))
        session.add(InterviewTurn(interview_run_id=run.id, turn_index=2,
                                  interviewer_text="How old are you?",
                                  candidate_text="Why does that matter?",
                                  interviewer_meta_json={"topic": "logistics"}))
        session.flush()
        results = analyze_run(session, run.id)
        assert results["exact_duplicate_questions"] == 1
        assert results["protected_characteristic_question_count"] == 1
        rows = session.execute(select(DeterministicMeasurement).where(
            DeterministicMeasurement.interview_run_id == run.id)).scalars().all()
        assert all(r.method for r in rows)


def test_statistics_bootstrap_and_labels():
    from services.statistics_service import (bootstrap_ci, compare_arms,
                                             pareto_frontier, result_label)
    deltas = [4.0, 5.0, 3.5, 6.0, 4.5]
    ci = bootstrap_ci(deltas)
    assert ci["lower"] is not None and ci["lower"] > 0
    assert result_label(deltas, 3.0, False, repetitions=3) == "confirmed_within_suite"
    assert result_label(deltas, 3.0, True, repetitions=3) == "safety_rejected"
    assert result_label([], 3.0, False, 2) == "inconclusive"
    control = {"m1": {"s1": 70, "s2": 72}}
    variant = {"m1": {"s1": 76, "s2": 77}}
    out = compare_arms(control, variant, repetitions=2)
    assert out["overall"]["mean_delta"] > 0
    frontier = pareto_frontier([
        {"name": "a", "quality": 80, "cost": 0.1},
        {"name": "b", "quality": 70, "cost": 0.2}])
    assert not frontier[0]["dominated"] and frontier[1]["dominated"]


def test_prompt_render_and_diff(seeded):
    from db import session_scope
    from services import prompts as prompt_service
    assert prompt_service.render("Hello {name} {missing}", {"name": "X"}) == \
        "Hello X {missing}"
    with session_scope() as session:
        module = prompt_service.get_or_create_module(session, "t.mod", "general")
        v1 = prompt_service.create_version(session, module, "line one", created_by="human")
        v2 = prompt_service.create_version(session, module, "line two", created_by="human")
        diff = prompt_service.diff_versions(v1, v2)
        assert "-line one" in diff and "+line two" in diff
        restored = prompt_service.restore_as_new_version(session, module, v1)
        assert restored.version == 3
        assert restored.template_text == "line one"


def test_memory_fact_grounding():
    from services.memory_strategies import MemoryState, apply_delta
    state = MemoryState(strategy="evior_candidate_memory_v2")
    result = apply_delta(
        state,
        {"facts": {"years_experience": "9", "employer": "NASA"},
         "skills": [{"name": "python", "probed_depth": 2}]},
        transcript_text="I have 9 years of experience building services.",
        source_docs="")
    assert "fact:years_experience" in result["accepted"]
    assert "fact:employer" in result["rejected_ungrounded"]
    assert state.facts.get("employer") is None
    # probed_depth never silently downgraded
    apply_delta(state, {"skills": [{"name": "python", "probed_depth": 0}]},
                "x", "")
    assert state.skills[0]["probed_depth"] == 2


def test_production_mode_selection_reimplementation():
    from adapters import choose_mode_for_product
    assert choose_mode_for_product(0) == "hr"
    assert choose_mode_for_product(2) == "technical"
    assert choose_mode_for_product(4) == "interviu_to_resume"
    assert choose_mode_for_product(3, time_used=0) == "hr"
    assert choose_mode_for_product(3, time_used=15, product_time=20) == "technical"
    campaign = [{"id": "hr-interview", "time": 10},
                {"id": "technical-interview", "time": 10}]
    assert choose_mode_for_product(8, 0, 0, campaign) == "hr"
    assert choose_mode_for_product(8, 10, 0, campaign) == "technical"
    assert choose_mode_for_product(8, 0, 5, campaign) == "technical"


def test_single_scenario_driver_warning():
    from services.statistics_service import compare_arms
    # Broad improvement across scenarios -> no warning
    control = {"m1": {"s1": 70, "s2": 72, "s3": 71}}
    variant = {"m1": {"s1": 75, "s2": 76, "s3": 74}}
    assert not compare_arms(control, variant)["overall"]["single_scenario_driver_warning"]
    # One big win hides small losses elsewhere -> warning
    control = {"m1": {"s1": 60, "s2": 72, "s3": 71}}
    variant = {"m1": {"s1": 85, "s2": 70, "s3": 69}}
    assert compare_arms(control, variant)["overall"]["single_scenario_driver_warning"]
