"""Bounded autonomous research cycle.

Hard limits enforced: max rounds, max variants per round, max total
experiments, holdout reserve untouchable, no evaluator-prompt mutation, no
holdout truth access (Research Director has no hidden-truth accessor).
"""
from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from config import get_config
from models import (Architecture, ArchitectureVersion, Experiment, Hypothesis,
                    ResearchDecision, ResearchRun, RubricVersion, ScenarioSuite,
                    ScenarioSuiteMember, TimelineEvent)
from schemas import (ExperimentDesignSchema, MutationSchema,
                     ResearchDecisionSchema)
from services import architectures as arch_service
from services import budgets
from services.evaluation import UNIVERSAL_METRICS
from services.experiments import create_experiment, launch
from services.provider import CallContext, call_llm
from orchestration import run_experiment_pipeline

MUTABLE_COMPONENTS = [
    "candidate_context_strategy", "follow_up_policy", "planning_strategy",
    "memory_strategy", "question_policy", "probing_policy", "evidence_tracking",
    "transcript_access",
]

COMPONENT_OPTIONS = {
    "candidate_context_strategy": ["raw_documents", "generated_brief"],
    "follow_up_policy": ["generic", "evidence_gap_tracking"],
    "planning_strategy": ["none", "fixed_plan", "adaptive_plan"],
    "memory_strategy": ["full_transcript", "rolling_window", "structured_summary",
                        "event_based", "evior_candidate_memory_v2", "hybrid"],
    "question_policy": ["template_driven", "goal_driven"],
    "probing_policy": ["basic", "depth_ladder"],
    "evidence_tracking": ["none", "structured_ledger"],
    "transcript_access": ["full", "rolling_window"],
}


def _development_personas(session: Session) -> list[str]:
    suite = session.execute(select(ScenarioSuite).where(
        ScenarioSuite.key == "development")).scalars().first()
    if not suite:
        return []
    return [m.persona_version_id for m in session.execute(
        select(ScenarioSuiteMember).where(
            ScenarioSuiteMember.suite_id == suite.id)).scalars()][:2]


def run_autonomous_cycle(session: Session, research_run_id: str) -> None:
    cfg = get_config()
    research_run = session.get(ResearchRun, research_run_id)
    project_id = research_run.project_id
    log: list[dict] = list(research_run.log_json or [])
    consecutive_failures: dict[str, int] = {}
    no_improvement_rounds = 0
    rubric = session.execute(select(RubricVersion)).scalars().first()

    def stop(reason: str, status: str = "completed") -> None:
        research_run.status = status
        research_run.stop_reason = reason
        research_run.log_json = log
        session.add(TimelineEvent(project_id=project_id, event_type="research_run_stopped",
                                  title=f"Autonomous cycle stopped: {reason}",
                                  detail_json={"research_run_id": research_run_id},
                                  actor="research_director"))
        session.commit()

    for round_index in range(min(research_run.max_rounds, cfg.max_autonomous_rounds)):
        session.refresh(research_run)
        if research_run.status in ("cancelled", "paused"):
            return stop("manual pause/cancel", research_run.status)
        if research_run.experiments_launched >= cfg.max_autonomous_experiments:
            return stop("max autonomous experiments reached")
        budget = budgets.get_budget(session, project_id)
        remaining = (budget.total_usd - budget.spent_usd - budget.reserved_usd
                     - (0 if budget.holdout_unlocked else budget.holdout_reserve_usd))
        if remaining < research_run.max_budget_usd * 0.1 or remaining <= 0.05:
            return stop("remaining budget would violate the holdout reserve",
                        "stopped_budget")

        # --- pick parent architecture first (needed for context building)
        parent = session.execute(
            select(ArchitectureVersion).join(
                Architecture, Architecture.id == ArchitectureVersion.architecture_id)
            .where(ArchitectureVersion.status.in_(("active", "promoted")))
            .order_by(ArchitectureVersion.created_at)).scalars().first()
        if parent is None:
            return stop("no active parent architecture")

        # --- Research Director decision (no holdout truth accessor exists here)
        previous = [e.result_label for e in session.execute(
            select(Experiment).where(Experiment.project_id == project_id,
                                     Experiment.result_label.isnot(None))).scalars()]
        # Build a summary of already-mutated components from existing architecture versions
        existing_variants = session.execute(
            select(ArchitectureVersion).where(
                ArchitectureVersion.status.in_(("active", "promoted", "rejected"))
            )).scalars().all()
        parent_components = parent.components_json
        already_tested = []
        for av in existing_variants:
            if av.id == parent.id:
                continue
            for comp, val in av.components_json.items():
                if comp in MUTABLE_COMPONENTS and val != parent_components.get(comp):
                    already_tested.append(f"{comp}={val}")
        already_tested = list(set(already_tested))

        director_messages = [
            {"role": "system", "content":
                "SYSTEM INSTRUCTIONS\n"
                "You are the Research Director for an interview-architecture lab.\n"
                "Choose the next single-component experiment to run.\n\n"
                "CONSTRAINTS:\n"
                "- You cannot alter objectives, rubrics, or hard constraints.\n"
                "- You cannot exceed budget.\n"
                "- You never see holdout data.\n\n"
                "OUTPUT FORMAT (strict JSON, no markdown, no code blocks):\n"
                "{\n"
                "  \"decision\": \"<one of: run_experiment, validate, stop, request_human_review>\",\n"
                "  \"research_question\": \"<string describing what you want to learn>\",\n"
                "  \"rationale\": \"<string explaining why>\",\n"
                "  \"target_components\": [\"<component name from mutable components list>\"],\n"
                "  \"candidate_parent_architectures\": [],\n"
                "  \"suggested_scenario_tags\": [],\n"
                "  \"maximum_budget\": 0.5,\n"
                "  \"stopping_reason\": null\n"
                "}\n\n"
                "VALID decision VALUES:\n"
                "  run_experiment  — launch a new experiment variant\n"
                "  validate        — re-run existing architecture to validate results\n"
                "  stop            — no more experiments needed\n"
                "  request_human_review — needs human judgment before proceeding\n\n"
                "IMPORTANT: 'decision' must be exactly one of those four strings. "
                "Do NOT put a component name in the 'decision' field."},
            {"role": "user", "content":
                f"Previous experiment labels: {previous}\n"
                f"Remaining budget: ${remaining:.2f}\n"
                f"Mutable components: {json.dumps(COMPONENT_OPTIONS)}\n"
                f"Already tested variants (DO NOT repeat these): {already_tested}\n\n"
                "Pick one component from the mutable components list that has NOT already been tested. "
                "Set decision='run_experiment' and put the component name in target_components. "
                "Do not pick any component listed in 'Already tested variants'."}]
        ctx = CallContext(session=session, project_id=project_id,
                          agent_role="research_director")
        decision_result = call_llm(ctx, director_messages, cfg.low_cost_model,
                                   schema=ResearchDecisionSchema,
                                   handoff_to="architecture_designer")
        decision: ResearchDecisionSchema = decision_result.parsed
        session.add(ResearchDecision(project_id=project_id,
                                     research_run_id=research_run_id,
                                     agent_role="research_director",
                                     decision_json=decision.model_dump()))
        log.append({"round": round_index, "decision": decision.decision,
                    "question": decision.research_question})
        if decision.decision == "stop":
            return stop(decision.stopping_reason or "director stop")
        if decision.decision == "request_human_review":
            return stop("director requested human review", "needs_human_review")

        # --- pick component (bounded; duplicates blocked)
        # parent already fetched above
        target = next((c for c in decision.target_components
                       if c in MUTABLE_COMPONENTS), MUTABLE_COMPONENTS[0])
        if consecutive_failures.get(target, 0) >= 2:
            return stop(f"2 consecutive failed experiments on component '{target}'")
        current_value = parent.components_json.get(target)
        options = [o for o in COMPONENT_OPTIONS.get(target, []) if o != current_value]
        if not options:
            return stop(f"no untested option for component '{target}'")
        hypothesis = Hypothesis(
            project_id=project_id, research_question=decision.research_question,
            statement=f"Changing {target} from {current_value} to {options[0]} improves "
                      "interview quality without unacceptable cost.",
            independent_variable=target, expected_effect="quality improvement",
            created_by="research_director")
        session.add(hypothesis)
        session.flush()
        try:
            mutation = MutationSchema(
                parent_architecture_id=parent.id, hypothesis_id=hypothesis.id,
                mutation_scope="single_component", target_component=target,
                proposed_after={target: options[0]},
                reason=decision.rationale[:500],
                risk_assessment=["autonomous mutation — bounded"],
                requires_human_approval=False)
            child = arch_service.apply_mutation(
                session, mutation, project_id, actor="agent",
                round_variant_count=0,
                max_variants_per_round=cfg.max_variants_per_round)
        except arch_service.ArchitectureError as exc:
            log.append({"round": round_index, "mutation_blocked": str(exc)})
            no_improvement_rounds += 1
            continue
        arch_service.set_status(session, child, "active",
                                reason="autonomous variant", actor="agent")

        personas = _development_personas(session)
        if not personas:
            return stop("no development personas seeded")
        per_round_budget = min(research_run.max_budget_usd
                               / max(1, research_run.max_rounds), remaining)
        design = ExperimentDesignSchema(
            research_question=decision.research_question,
            hypothesis_id=hypothesis.id,
            control_architecture_version_id=parent.id,
            variant_architecture_version_id=child.id,
            independent_variable=target,
            dependent_metrics=UNIVERSAL_METRICS[:8],
            scenario_persona_version_ids=personas,
            interview_modes=["HR"], repetitions=1,
            max_cost_usd=round(per_round_budget, 2),
            rubric_version_id=rubric.id if rubric else "",
            model_config_lock={"interview_model": cfg.interview_model,
                               "evaluator_model": cfg.evaluator_model,
                               "max_turns": 4})
        experiment = create_experiment(
            session, project_id, f"[auto r{round_index}] {target} → {options[0]}",
            design, hypothesis_id=hypothesis.id, research_run_id=research_run_id)
        session.commit()
        launch(session, experiment)
        session.commit()
        # execute inline (worker already owns this job)
        run_experiment_pipeline(session, experiment)
        session.refresh(experiment)
        research_run.experiments_launched += 1
        research_run.rounds_completed = round_index + 1
        label = experiment.result_label or "inconclusive"
        log.append({"round": round_index, "experiment_id": experiment.id,
                    "label": label, "cost": experiment.actual_cost_usd})
        if label in ("rejected", "inconclusive", "safety_rejected"):
            consecutive_failures[target] = consecutive_failures.get(target, 0) + 1
            no_improvement_rounds += 1
            if label == "safety_rejected":
                arch_service.set_status(session, child, "safety_rejected",
                                        reason="autonomous cycle", actor="agent")
                return stop("unresolved safety/fairness failure", "stopped_safety")
        else:
            consecutive_failures[target] = 0
            no_improvement_rounds = 0
            if label in ("promising", "confirmed_within_suite"):
                arch_service.set_status(session, child, "promoted",
                                        reason=f"experiment {experiment.id} label={label}",
                                        actor="agent")
        if no_improvement_rounds >= 3:
            return stop("3 rounds with no meaningful improvement")
        research_run.log_json = log
        session.commit()

    stop("max autonomous rounds completed")
