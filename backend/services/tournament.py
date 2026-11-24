"""Autonomous pipeline tournament.

What it does, in order:
  1. IDEATE  — generate K diverse pipeline designs (multi-stage AI pipelines,
               optionally with web-research stages). LLM-generated in real
               provider mode; a curated labeled set in stub mode.
  2. IMPROVE — each pipeline self-improves for R rounds: an improvement agent
               proposes ONE bounded stage mutation, a controlled experiment
               tests child vs current champion on the same scenarios, the
               winner becomes that pipeline's champion.
  3. COMPETE — champions face the reference baseline sequentially (king-of-
               the-hill on shared scenarios); the surviving best is reported.

Everything reuses the lab's existing scientific machinery: pipelines are
architecture versions, comparisons are experiments with blinded evaluation,
every model/web call is budget-gated, and all hard limits still apply.
The tournament NEVER touches evaluators, rubrics, hidden truth, or holdout.
"""
from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from config import get_config
from models import (Architecture, ArchitectureVersion, Experiment, Hypothesis,
                    ResearchRun, RubricVersion, ScenarioSuite,
                    ScenarioSuiteMember, TimelineEvent)
from schemas import ExperimentDesignSchema
from services import architectures as arch_service
from services import budgets
from services.evaluation import UNIVERSAL_METRICS
from services.experiments import create_experiment, launch
from services.pipelines import (PipelineIdeasSchema, PipelineMutationSchema,
                                PipelineSpec, PipelineStage,
                                PipelineValidationError,
                                apply_pipeline_mutation, validate_pipeline)
from services.provider import CallContext, SchemaValidationFailed, call_llm
from orchestration import run_experiment_pipeline

MAX_PIPELINES = 5
MAX_IMPROVE_ROUNDS = 4

# Curated ideas used in stub provider mode (labeled; deterministic).
STUB_IDEAS = [
    PipelineSpec(name="Plain interviewer", description="No extra stages — reference point.",
                 pre_stages=[], per_turn_stages=[]),
    PipelineSpec(name="Research-first interviewer",
                 description="Web-researches company and field before interviewing.",
                 pre_stages=[PipelineStage(stage_type="company_research"),
                             PipelineStage(stage_type="field_research")],
                 per_turn_stages=[]),
    PipelineSpec(name="Brief + plan pipeline",
                 description="Structures a role brief, then drafts a plan, then interviews.",
                 pre_stages=[PipelineStage(stage_type="role_brief"),
                             PipelineStage(stage_type="interview_plan")],
                 per_turn_stages=[]),
    PipelineSpec(name="Strategist-guided interviewer",
                 description="A silent strategist picks the next target every turn.",
                 pre_stages=[PipelineStage(stage_type="role_brief")],
                 per_turn_stages=[PipelineStage(stage_type="question_strategist")]),
    PipelineSpec(name="Full analytic pipeline",
                 description="Research + brief + plan + per-turn analyzer.",
                 pre_stages=[PipelineStage(stage_type="technique_research"),
                             PipelineStage(stage_type="role_brief"),
                             PipelineStage(stage_type="interview_plan")],
                 per_turn_stages=[PipelineStage(stage_type="answer_analyzer")]),
]


def _dev_personas(session: Session, n: int = 2) -> list[str]:
    suite = session.execute(select(ScenarioSuite).where(
        ScenarioSuite.key == "development")).scalars().first()
    if not suite:
        return []
    return [m.persona_version_id for m in session.execute(
        select(ScenarioSuiteMember).where(
            ScenarioSuiteMember.suite_id == suite.id)).scalars()][:n]


def _remaining_budget(session: Session, project_id: str) -> float:
    b = budgets.get_budget(session, project_id)
    return (b.total_usd - b.spent_usd - b.reserved_usd
            - (0 if b.holdout_unlocked else b.holdout_reserve_usd))


def _reference_version(session: Session) -> ArchitectureVersion | None:
    """A5 (evidence ledger) if present, else A1, else any active version."""
    for code in ("A5", "A1", "A0"):
        arch = session.execute(select(Architecture).where(
            Architecture.code == code)).scalars().first()
        if arch:
            v = session.execute(select(ArchitectureVersion).where(
                ArchitectureVersion.architecture_id == arch.id)
                .order_by(ArchitectureVersion.version.desc())).scalars().first()
            if v:
                return v
    return session.execute(select(ArchitectureVersion)).scalars().first()


def generate_pipeline_ideas(session: Session, project_id: str, k: int,
                            allow_web: bool) -> list[PipelineSpec]:
    cfg = get_config()
    k = max(2, min(k, MAX_PIPELINES))
    if cfg.provider_mode == "stub":
        ideas = [i for i in STUB_IDEAS
                 if allow_web or not any(s.stage_type.endswith("_research")
                                         for s in i.pre_stages)]
        return ideas[:k]
    ctx = CallContext(session=session, project_id=project_id,
                      agent_role="pipeline_idea_generator")
    web_note = ("Web-research stages ARE available." if allow_web else
                "Web-research stages are NOT available — do not use them.")
    result = call_llm(ctx, [
        {"role": "system", "content":
            "SYSTEM INSTRUCTIONS\nYou design multi-stage AI interview pipelines for a "
            "research tournament. Each pipeline is stages around a core interviewer.\n"
            "Allowed pre_stages types: company_research, field_research, "
            "technique_research (web); role_brief, interview_plan (llm). Max 3.\n"
            "Allowed per_turn_stages types: question_strategist, answer_analyzer. Max 2.\n"
            f"{web_note}\n"
            "Make the ideas genuinely DIFFERENT from each other (simple to advanced). "
            "Each stage may carry short custom 'instructions' (its skill). "
            'Output JSON: {"ideas": [{"name", "description", '
            '"pre_stages": [{"stage_type", "instructions"}], '
            '"per_turn_stages": [{"stage_type", "instructions"}]}]}'},
        {"role": "user", "content":
            f"Generate {k} diverse pipeline designs for AI job interviews. Include at "
            "least one minimal pipeline (no stages) as a reference and at least one "
            "advanced multi-stage design."}],
        cfg.low_cost_model, schema=PipelineIdeasSchema, max_output_tokens=1400)
    ideas: list[PipelineSpec] = []
    for idea in result.parsed.ideas[:k]:
        try:
            ideas.append(validate_pipeline(idea))
        except PipelineValidationError:
            continue  # drop invalid ideas rather than failing the tournament
    return ideas or STUB_IDEAS[:2]


def _propose_mutation(session: Session, project_id: str, spec: PipelineSpec,
                      round_index: int, previous_results: list[str]) -> PipelineSpec:
    cfg = get_config()
    if cfg.provider_mode == "stub":
        # deterministic bounded tweak: sharpen instructions of first stage,
        # or add a strategist if there is room.
        data = spec.model_dump()
        if data["pre_stages"]:
            data["pre_stages"][0]["instructions"] = (
                (data["pre_stages"][0]["instructions"] + " Focus on deal-breaker "
                 "requirements and quantified outcomes.")[:790])
        elif len(data["per_turn_stages"]) < 2:
            data["per_turn_stages"].append(
                {"stage_type": "question_strategist",
                 "instructions": "Target the weakest-evidence competency."})
        else:
            data["per_turn_stages"][0]["instructions"] = (
                (data["per_turn_stages"][0]["instructions"] + " Prefer depth over "
                 "breadth.")[:790])
        return validate_pipeline(PipelineSpec.model_validate(data))
    ctx = CallContext(session=session, project_id=project_id,
                      agent_role="pipeline_improvement_agent")
    result = call_llm(ctx, [
        {"role": "system", "content":
            "SYSTEM INSTRUCTIONS\nYou improve ONE interview pipeline with ONE bounded "
            "mutation: edit a stage's instructions, add a stage, or remove a stage. "
            "Allowed pre types: company_research, field_research, technique_research, "
            "role_brief, interview_plan. Allowed per-turn types: question_strategist, "
            "answer_analyzer. Output JSON matching: {\"action\": \"edit_stage|add_stage|"
            "remove_stage\", \"section\": \"pre_stages|per_turn_stages\", "
            "\"stage_index\": 0, \"new_stage\": {\"stage_type\", \"instructions\"}, "
            "\"reason\": str}"},
        {"role": "user", "content":
            f"Improvement round {round_index}. Current pipeline:\n"
            + json.dumps(spec.model_dump(), ensure_ascii=False)
            + f"\nPrevious experiment outcomes for this pipeline: {previous_results}"}],
        cfg.low_cost_model, schema=PipelineMutationSchema, max_output_tokens=400)
    return apply_pipeline_mutation(spec, result.parsed)


def _register_pipeline(session: Session, project_id: str, spec: PipelineSpec,
                       tournament_id: str, base: ArchitectureVersion,
                       parent_version: ArchitectureVersion | None = None,
                       round_index: int = 0) -> ArchitectureVersion:
    suffix = f"t{tournament_id[:6]}"
    name = f"[pipeline] {spec.name} ({suffix}, r{round_index})"
    existing = session.execute(select(Architecture).where(
        Architecture.name == name)).scalars().first()
    arch = existing or arch_service.create_architecture(
        session, name=name, code="", description=spec.description,
        creation_source="agent")
    components = dict(base.components_json)
    components["pipeline_stages"] = spec.model_dump()
    version = arch_service.create_version(
        session, arch, components=components,
        model_config=dict(base.model_config_json),
        limits=dict(base.limits_json),
        interview_modes=list(base.interview_mode_support_json),
        prompt_module_versions=dict(base.prompt_module_versions_json),
        parent_version_id=(parent_version.id if parent_version else base.id),
        change_description=f"pipeline tournament {suffix} round {round_index}",
        status="active", actor="agent")
    return version


def _compare(session: Session, project_id: str, name: str,
             control: ArchitectureVersion, variant: ArchitectureVersion,
             personas: list[str], mode: str, per_experiment_budget: float,
             rubric_id: str, research_run_id: str) -> tuple[str, float]:
    """Run one controlled experiment; returns (label, mean_delta)."""
    cfg = get_config()
    hypothesis = Hypothesis(
        project_id=project_id, research_question=name,
        statement="Variant pipeline outperforms control on quality metrics.",
        independent_variable="pipeline_stages (bundled pipeline intervention)",
        created_by="pipeline_tournament")
    session.add(hypothesis)
    session.flush()
    design = ExperimentDesignSchema(
        research_question=name, hypothesis_id=hypothesis.id,
        control_architecture_version_id=control.id,
        variant_architecture_version_id=variant.id,
        independent_variable="pipeline_stages (bundled pipeline intervention)",
        dependent_metrics=UNIVERSAL_METRICS[:8],
        scenario_persona_version_ids=personas, interview_modes=[mode],
        repetitions=1, max_cost_usd=round(per_experiment_budget, 2),
        rubric_version_id=rubric_id,
        model_config_lock={"interview_model": cfg.interview_model,
                           "evaluator_model": cfg.evaluator_model,
                           "max_turns": 4})
    experiment = create_experiment(session, project_id, name, design,
                                   hypothesis_id=hypothesis.id,
                                   research_run_id=research_run_id)
    session.commit()
    launch(session, experiment)
    # Executed inline by the tournament — complete the queued job so a second
    # worker never double-runs (and double-charges) this experiment.
    from models import BackgroundJob
    queued = session.execute(select(BackgroundJob).where(
        BackgroundJob.job_type == "run_experiment",
        BackgroundJob.subject_id == experiment.id,
        BackgroundJob.status == "queued")).scalars().first()
    if queued:
        queued.status = "completed"
        queued.claimed_by = "pipeline_tournament_inline"
    session.commit()
    run_experiment_pipeline(session, experiment)
    session.refresh(experiment)
    label = experiment.result_label or "inconclusive"
    overall = ((experiment.decision_json or {}).get("comparison", {})
               .get("overall", {}))
    mean_delta = overall.get("mean_delta") or 0.0
    hc = (experiment.decision_json or {}).get("comparison", {}).get(
        "hard_constraints", {})
    if hc and hc.get("variant", 0) > hc.get("control", 0):
        return "safety_rejected", mean_delta
    return label, float(mean_delta)


def run_pipeline_tournament(session: Session, research_run_id: str) -> None:
    research_run = session.get(ResearchRun, research_run_id)
    project_id = research_run.project_id
    params = (research_run.log_json or [{}])[0] if research_run.log_json else {}
    num_pipelines = int(params.get("num_pipelines", 3))
    improve_rounds = min(int(params.get("improve_rounds", 1)), MAX_IMPROVE_ROUNDS)
    mode = params.get("interview_mode", "HR")
    allow_web = bool(params.get("allow_web", True))
    log: list = list(research_run.log_json or [])
    rubric = session.execute(select(RubricVersion)).scalars().first()

    def save(status: str | None = None, stop_reason: str = "") -> None:
        research_run.log_json = list(log)
        if status:
            research_run.status = status
            research_run.stop_reason = stop_reason
        session.commit()

    def budget_left() -> float:
        spent = sum(e.actual_cost_usd for e in session.execute(
            select(Experiment).where(
                Experiment.research_run_id == research_run_id)).scalars())
        return min(research_run.max_budget_usd - spent,
                   _remaining_budget(session, project_id))

    base = _reference_version(session)
    if base is None:
        return save("completed", "no base architecture — seed the lab first")
    personas = _dev_personas(session)
    if not personas:
        return save("completed", "no development personas — seed the lab first")

    # Estimate-aware per-experiment budget: cap each experiment at ~120% of a
    # real cost estimate so launches are never blocked by an arbitrary split;
    # the tournament simply runs fewer experiments when the budget is tight.
    from services import experiments as exp_service
    total_experiments = num_pipelines * improve_rounds + num_pipelines
    probe_design = ExperimentDesignSchema(
        research_question="tournament cost probe", hypothesis_id="",
        control_architecture_version_id=base.id,
        variant_architecture_version_id=base.id,
        independent_variable="pipeline_stages (bundled pipeline intervention)",
        dependent_metrics=UNIVERSAL_METRICS[:8],
        scenario_persona_version_ids=personas, interview_modes=[mode],
        repetitions=1, max_cost_usd=max(0.01, research_run.max_budget_usd),
        model_config_lock={"interview_model": get_config().interview_model,
                           "evaluator_model": get_config().evaluator_model,
                           "max_turns": 4})
    single_estimate = exp_service.estimate_cost(
        session, probe_design)["estimated_total_usd"]
    if single_estimate > research_run.max_budget_usd:
        return save("needs_human_review",
                    f"tournament budget too small: ONE comparison experiment is "
                    f"estimated at ~${single_estimate:.2f} but the tournament budget "
                    f"is ${research_run.max_budget_usd:.2f}. Increase the budget, "
                    f"or lower costs (fewer max_turns, cheaper models in the "
                    f"pricing table).")
    per_experiment = max(0.2, round(single_estimate * 1.2, 2))
    log.append({"phase": "budget_plan",
                "single_experiment_estimate_usd": round(single_estimate, 3),
                "per_experiment_cap_usd": per_experiment,
                "planned_experiments": total_experiments,
                "affordable_experiments": int(research_run.max_budget_usd
                                              // per_experiment),
                "note": "runs until the tournament budget is exhausted; later "
                        "experiments are skipped if funds run out"})
    save()

    # ---------- Phase 1: ideate
    log.append({"phase": "ideate", "num_pipelines": num_pipelines,
                "allow_web": allow_web})
    save()
    try:
        ideas = generate_pipeline_ideas(session, project_id, num_pipelines, allow_web)
    except (SchemaValidationFailed, budgets.BudgetExceededError) as exc:
        return save("needs_human_review", f"idea generation failed: {exc}")
    champions: list[dict] = []
    for idea in ideas:
        version = _register_pipeline(session, project_id, idea,
                                     research_run_id, base)
        champions.append({"name": idea.name, "spec": idea.model_dump(),
                          "version_id": version.id, "history": []})
        log.append({"phase": "registered", "pipeline": idea.name,
                    "version_id": version.id,
                    "stages": {"pre": [s.stage_type for s in idea.pre_stages],
                               "per_turn": [s.stage_type for s in idea.per_turn_stages]}})
    session.commit()
    save()

    # ---------- Phase 2: each pipeline improves itself
    for champ in champions:
        for round_index in range(1, improve_rounds + 1):
            session.refresh(research_run)
            if research_run.status in ("cancelled", "paused"):
                return save(research_run.status, "manual pause/cancel")
            if budget_left() < per_experiment:
                log.append({"phase": "improve_stopped", "reason": "budget"})
                save()
                break
            spec = PipelineSpec.model_validate(champ["spec"])
            try:
                mutated = _propose_mutation(session, project_id, spec,
                                            round_index, champ["history"])
            except (SchemaValidationFailed, PipelineValidationError) as exc:
                champ["history"].append(f"r{round_index}: mutation invalid ({exc})")
                continue
            if mutated.model_dump() == spec.model_dump():
                champ["history"].append(f"r{round_index}: no-op mutation skipped")
                continue
            current = session.get(ArchitectureVersion, champ["version_id"])
            child = _register_pipeline(session, project_id, mutated,
                                       research_run_id, base,
                                       parent_version=current,
                                       round_index=round_index)
            try:
                label, delta = _compare(
                    session, project_id,
                    f"[tournament] {champ['name']} self-improve r{round_index}",
                    current, child, personas, mode, per_experiment,
                    rubric.id if rubric else "", research_run_id)
            except budgets.BudgetExceededError:
                log.append({"phase": "improve_stopped", "reason": "budget cap hit"})
                save()
                break
            research_run.experiments_launched += 1
            improved = label not in ("safety_rejected", "rejected") and delta > 0
            if improved:
                arch_service.set_status(session, child, "promoted",
                                        reason=f"tournament self-improve Δ={delta}",
                                        actor="agent")
                champ["version_id"] = child.id
                champ["spec"] = mutated.model_dump()
            else:
                arch_service.set_status(session, child, "rejected",
                                        reason=f"tournament self-improve Δ={delta} "
                                               f"label={label}", actor="agent")
            champ["history"].append(
                f"r{round_index}: {'improved' if improved else 'kept previous'} "
                f"(Δ={delta}, {label})")
            log.append({"phase": "improve", "pipeline": champ["name"],
                        "round": round_index, "label": label, "delta": delta,
                        "champion_version": champ["version_id"]})
            save()

    # ---------- Phase 3: champions compete (king of the hill vs baseline)
    best = {"name": "reference baseline", "version_id": base.id}
    for champ in champions:
        session.refresh(research_run)
        if research_run.status in ("cancelled", "paused"):
            return save(research_run.status, "manual pause/cancel")
        if budget_left() < per_experiment:
            log.append({"phase": "final_stopped", "reason": "budget"})
            break
        if champ["version_id"] == best["version_id"]:
            continue
        control = session.get(ArchitectureVersion, best["version_id"])
        challenger = session.get(ArchitectureVersion, champ["version_id"])
        try:
            label, delta = _compare(
                session, project_id,
                f"[tournament final] {best['name']} vs {champ['name']}",
                control, challenger, personas, mode, per_experiment,
                rubric.id if rubric else "", research_run_id)
        except budgets.BudgetExceededError:
            log.append({"phase": "final_stopped", "reason": "budget cap hit"})
            break
        research_run.experiments_launched += 1
        won = label not in ("safety_rejected", "rejected") and delta > 0
        log.append({"phase": "final", "challenger": champ["name"],
                    "against": best["name"], "label": label, "delta": delta,
                    "winner": champ["name"] if won else best["name"]})
        if won:
            best = {"name": champ["name"], "version_id": champ["version_id"]}
        save()

    log.append({"phase": "result", "best_pipeline": best["name"],
                "best_version_id": best["version_id"],
                "note": ("Best-supported pipeline WITHIN this tournament's ideas, "
                         "scenarios, repetitions and budget — not a global optimum. "
                         "Validate on regression/holdout before adopting.")})
    session.add(TimelineEvent(
        project_id=project_id, event_type="pipeline_tournament_finished",
        title=f"Pipeline tournament finished — best: {best['name']}",
        detail_json={"research_run_id": research_run_id,
                     "best_version_id": best["version_id"]},
        actor="pipeline_tournament"))
    research_run.rounds_completed = improve_rounds
    save("completed", f"tournament finished; best pipeline: {best['name']}")
