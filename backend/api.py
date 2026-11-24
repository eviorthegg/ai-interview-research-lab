"""REST API: /api/v1/research-lab. Consistent envelopes {data, meta, errors}."""
from __future__ import annotations

import json
import time

from flask import Blueprint, Response, jsonify, request
from pydantic import ValidationError
from sqlalchemy import select

from config import get_config
from db import session_scope
from models import (Architecture, ArchitectureVersion, BackgroundJob, Budget,
                    CostEvent, CriticReview, DeterministicMeasurement,
                    EvaluationRun, Experiment, ExperimentScenario,
                    FinalRecommendation, HardConstraintFinding, Hypothesis,
                    HumanReviewItem, InterviewRun, InterviewStateSnapshot,
                    InterviewTurn, JudgeDisagreement, MemoryEvent,
                    MemorySnapshot, MetricScore, ModelCall, PairwiseComparison,
                    ProgressEvent, PromptModule, PromptVersion, ResearchProject,
                    ResearchRun, RubricVersion, ScenarioSuite,
                    ScenarioSuiteMember, TimelineEvent)
from repositories import (HiddenTruthAccessDenied, HiddenTruthRepository,
                          HoldoutLockedError, PersonaRepository)
from schemas import ExperimentDesignSchema, HypothesisSchema, MutationSchema
from services import architectures as arch_service
from services import budgets as budget_service
from services import experiments as exp_service
from services import prompts as prompt_service
from services.reporting import generate_final_recommendation, unlock_holdout_for_validation
from services.statistics_service import pareto_frontier
from services.experiments import ExperimentValidationError

api = Blueprint("api", __name__, url_prefix="/api/v1/research-lab")


def envelope(data=None, meta=None, errors=None, status=200):
    return jsonify({"data": data, "meta": meta or {}, "errors": errors or []}), status


def error_response(message, status=400, code="bad_request"):
    return envelope(errors=[{"code": code, "message": str(message)}], status=status)


@api.errorhandler(ValidationError)
def _validation(exc):
    return error_response(str(exc), 422, "schema_validation")


# ------------------------------------------------------------------ projects

@api.post("/projects")
def create_project():
    body = request.get_json(force=True)
    cfg = get_config()
    with session_scope() as session:
        project = ResearchProject(name=body["name"],
                                  objective=body.get("objective", ""))
        session.add(project)
        session.flush()
        session.add(Budget(
            project_id=project.id,
            total_usd=float(body.get("budget_usd", cfg.default_project_budget_usd)),
            holdout_reserve_usd=float(body.get("holdout_reserve_usd",
                                               cfg.final_validation_reserve_usd))))
        return envelope({"id": project.id, "name": project.name}, status=201)


@api.get("/projects")
def list_projects():
    with session_scope() as session:
        rows = session.execute(select(ResearchProject)).scalars().all()
        return envelope([{"id": p.id, "name": p.name, "status": p.status,
                          "current_phase": p.current_phase, "is_seeded": p.is_seeded,
                          "objective": p.objective} for p in rows])


@api.get("/projects/<project_id>")
def get_project(project_id):
    with session_scope() as session:
        p = session.get(ResearchProject, project_id)
        if not p:
            return error_response("not found", 404, "not_found")
        return envelope({"id": p.id, "name": p.name, "status": p.status,
                         "current_phase": p.current_phase, "objective": p.objective,
                         "is_seeded": p.is_seeded})


@api.patch("/projects/<project_id>")
def patch_project(project_id):
    body = request.get_json(force=True)
    with session_scope() as session:
        p = session.get(ResearchProject, project_id)
        if not p:
            return error_response("not found", 404, "not_found")
        for key in ("name", "objective", "status", "current_phase"):
            if key in body:
                setattr(p, key, body[key])
        return envelope({"id": p.id})


@api.get("/projects/<project_id>/budget")
def get_budget(project_id):
    with session_scope() as session:
        b = session.execute(select(Budget).where(
            Budget.project_id == project_id)).scalars().first()
        if not b:
            return error_response("not found", 404, "not_found")
        return envelope({
            "total_usd": b.total_usd, "spent_usd": round(b.spent_usd, 4),
            "reserved_usd": round(b.reserved_usd, 4),
            "holdout_reserve_usd": b.holdout_reserve_usd,
            "holdout_unlocked": b.holdout_unlocked,
            "available_usd": round(b.total_usd - b.spent_usd - b.reserved_usd
                                   - (0 if b.holdout_unlocked else b.holdout_reserve_usd), 4)})


@api.put("/projects/<project_id>/budget")
def put_budget(project_id):
    body = request.get_json(force=True)
    with session_scope() as session:
        b = session.execute(select(Budget).where(
            Budget.project_id == project_id)).scalars().first()
        if not b:
            return error_response("not found", 404, "not_found")
        if "total_usd" in body:
            new_total = float(body["total_usd"])
            if new_total < b.spent_usd + b.reserved_usd:
                return error_response(
                    f"Total budget cannot be set below already spent+reserved "
                    f"(${b.spent_usd + b.reserved_usd:.2f}).", 422, "budget_too_low")
            b.total_usd = new_total
        if "holdout_reserve_usd" in body:
            new_reserve = float(body["holdout_reserve_usd"])
            if new_reserve < 0 or new_reserve > b.total_usd:
                return error_response("Holdout reserve must be between 0 and the "
                                      "total budget.", 422, "reserve_invalid")
            b.holdout_reserve_usd = new_reserve
        if body.get("unlock_holdout") is True:
            budget_service.unlock_holdout(session, project_id)
        b.version += 1
        return envelope({"total_usd": b.total_usd,
                         "holdout_reserve_usd": b.holdout_reserve_usd,
                         "holdout_unlocked": b.holdout_unlocked})


@api.get("/projects/<project_id>/costs")
def get_costs(project_id):
    with session_scope() as session:
        rows = session.execute(select(CostEvent).where(
            CostEvent.project_id == project_id)
            .order_by(CostEvent.created_at.desc()).limit(500)).scalars().all()
        return envelope([{
            "id": c.id, "agent_role": c.agent_role, "model": c.model,
            "input_tokens": c.input_tokens, "output_tokens": c.output_tokens,
            "estimated_usd": c.estimated_usd, "actual_usd": c.actual_usd,
            "experiment_id": c.experiment_id, "interview_run_id": c.interview_run_id,
            "created_at": c.created_at.isoformat()} for c in rows])


# -------------------------------------------------------------- architectures

def _version_dict(session, v: ArchitectureVersion, arch: Architecture | None = None):
    arch = arch or session.get(Architecture, v.architecture_id)
    return {
        "architecture_id": v.architecture_id, "architecture_version_id": v.id,
        "name": arch.name if arch else "?", "code": arch.code if arch else "",
        "version": v.version, "status": v.status,
        "is_protected": arch.is_protected if arch else False,
        "creation_source": arch.creation_source if arch else "?",
        "parent_version_id": v.parent_version_id,
        "hypothesis_id": v.hypothesis_id,
        "change_description": v.change_description,
        "components": v.components_json, "model_config": v.model_config_json,
        "limits": v.limits_json, "interview_mode_support": v.interview_mode_support_json,
        "prompt_module_versions": v.prompt_module_versions_json,
        "configuration_hash": v.configuration_hash, "immutable": v.immutable,
        "created_at": v.created_at.isoformat()}


@api.get("/architectures")
def list_architectures():
    with session_scope() as session:
        out = []
        for arch in session.execute(select(Architecture)).scalars():
            versions = session.execute(select(ArchitectureVersion).where(
                ArchitectureVersion.architecture_id == arch.id)
                .order_by(ArchitectureVersion.version.desc())).scalars().all()
            for v in versions:
                out.append(_version_dict(session, v, arch))
        return envelope(out)


@api.post("/architectures")
def create_architecture():
    body = request.get_json(force=True)
    with session_scope() as session:
        arch = arch_service.create_architecture(
            session, name=body["name"], code=body.get("code", ""),
            description=body.get("description", ""),
            creation_source=body.get("creation_source", "human"))
        version = arch_service.create_version(
            session, arch, components=body.get("components", {}),
            model_config=body.get("model_config"), limits=body.get("limits"),
            interview_modes=body.get("interview_mode_support"),
            prompt_module_versions=body.get("prompt_module_versions"),
            change_description=body.get("change_description", "initial version"),
            status=body.get("status", "draft"))
        return envelope(_version_dict(session, version, arch), status=201)


@api.get("/architectures/<version_id>")
def get_architecture(version_id):
    with session_scope() as session:
        v = session.get(ArchitectureVersion, version_id)
        if not v:
            return error_response("not found", 404, "not_found")
        return envelope(_version_dict(session, v))


@api.post("/architectures/<version_id>/versions")
def mutate_architecture(version_id):
    body = request.get_json(force=True)
    body.setdefault("parent_architecture_id", version_id)
    mutation = MutationSchema.model_validate(body)
    with session_scope() as session:
        parent = session.get(ArchitectureVersion, version_id)
        if not parent:
            return error_response("not found", 404, "not_found")
        arch = session.get(Architecture, parent.architecture_id)
        project = session.execute(select(ResearchProject)).scalars().first()
        try:
            child = arch_service.apply_mutation(
                session, mutation, project.id if project else "",
                actor=body.get("actor", "human"))
        except arch_service.ArchitectureError as exc:
            return error_response(str(exc), 422, "mutation_invalid")
        return envelope(_version_dict(session, child), status=201)


@api.get("/architectures/<version_id>/lineage")
def get_lineage(version_id):
    with session_scope() as session:
        v = session.get(ArchitectureVersion, version_id)
        if not v:
            return error_response("not found", 404, "not_found")
        return envelope(arch_service.lineage(session, v))


@api.post("/architectures/<version_id>/promote")
def promote_architecture(version_id):
    body = request.get_json(silent=True) or {}
    with session_scope() as session:
        v = session.get(ArchitectureVersion, version_id)
        if not v:
            return error_response("not found", 404, "not_found")
        # promotion requires no unresolved hard-constraint findings
        run_ids = [r.id for r in session.execute(select(InterviewRun).where(
            InterviewRun.architecture_version_id == version_id)).scalars()]
        if run_ids:
            unresolved = session.execute(select(HardConstraintFinding).where(
                HardConstraintFinding.interview_run_id.in_(run_ids),
                HardConstraintFinding.resolved == False)).scalars().first()  # noqa: E712
            if unresolved:
                return error_response(
                    f"Cannot promote: unresolved hard-constraint finding "
                    f"'{unresolved.constraint_key}'.", 409, "hard_constraint")
        arch_service.set_status(session, v, "promoted",
                                reason=body.get("reason", ""), actor="human")
        return envelope({"status": v.status})


@api.post("/architectures/<version_id>/reject")
def reject_architecture(version_id):
    body = request.get_json(silent=True) or {}
    with session_scope() as session:
        v = session.get(ArchitectureVersion, version_id)
        if not v:
            return error_response("not found", 404, "not_found")
        arch_service.set_status(session, v, body.get("status", "rejected"),
                                reason=body.get("reason", ""), actor="human")
        return envelope({"status": v.status})


@api.get("/architectures/<version_id>/diff/<other_id>")
def diff_architectures(version_id, other_id):
    with session_scope() as session:
        a = session.get(ArchitectureVersion, version_id)
        b = session.get(ArchitectureVersion, other_id)
        if not a or not b:
            return error_response("not found", 404, "not_found")
        return envelope(arch_service.component_diff(a, b))


# -------------------------------------------------------------------- prompts

@api.get("/prompts")
def list_prompts():
    with session_scope() as session:
        out = []
        for module in session.execute(select(PromptModule)).scalars():
            versions = session.execute(select(PromptVersion).where(
                PromptVersion.module_id == module.id)
                .order_by(PromptVersion.version.desc())).scalars().all()
            out.append({
                "id": module.id, "key": module.key, "category": module.category,
                "is_protected": module.is_protected,
                "description": module.description,
                "versions": [{"id": v.id, "version": v.version, "status": v.status,
                              "content_hash": v.content_hash[:12],
                              "created_by": v.created_by,
                              "created_at": v.created_at.isoformat()}
                             for v in versions]})
        return envelope(out)


@api.post("/prompts")
def create_prompt():
    body = request.get_json(force=True)
    with session_scope() as session:
        module = prompt_service.get_or_create_module(
            session, key=body["key"], category=body.get("category", "general"),
            description=body.get("description", ""),
            is_protected=bool(body.get("is_protected", False)))
        version = prompt_service.create_version(
            session, module, body["template_text"],
            variables=body.get("variables"), created_by=body.get("created_by", "human"),
            status=body.get("status", "published"))
        return envelope({"module_id": module.id, "version_id": version.id,
                         "version": version.version}, status=201)


@api.post("/prompts/<module_id>/versions")
def add_prompt_version(module_id):
    body = request.get_json(force=True)
    with session_scope() as session:
        module = session.get(PromptModule, module_id)
        if not module:
            return error_response("not found", 404, "not_found")
        try:
            version = prompt_service.create_version(
                session, module, body["template_text"],
                variables=body.get("variables"),
                created_by=body.get("created_by", "human"),
                status=body.get("status", "published"))
        except prompt_service.ProtectedPromptError as exc:
            return error_response(str(exc), 403, "protected_prompt")
        return envelope({"version_id": version.id, "version": version.version}, status=201)


@api.get("/prompts/<module_id>/versions/<version_id>")
def get_prompt_version(module_id, version_id):
    with session_scope() as session:
        v = session.get(PromptVersion, version_id)
        if not v or v.module_id != module_id:
            return error_response("not found", 404, "not_found")
        return envelope({"id": v.id, "version": v.version, "status": v.status,
                         "template_text": v.template_text,
                         "content_hash": v.content_hash,
                         "created_by": v.created_by})


@api.get("/prompts/<module_id>/diff/<version_id>")
def diff_prompt(module_id, version_id):
    other_id = request.args.get("against")
    with session_scope() as session:
        a = session.get(PromptVersion, version_id)
        if not a:
            return error_response("not found", 404, "not_found")
        if other_id:
            b = session.get(PromptVersion, other_id)
        else:
            b = session.execute(select(PromptVersion).where(
                PromptVersion.module_id == module_id,
                PromptVersion.id != version_id)
                .order_by(PromptVersion.version.desc())).scalars().first()
        if not b:
            return error_response("no version to diff against", 404, "not_found")
        return envelope({"diff": prompt_service.diff_versions(b, a)})


@api.post("/prompts/<module_id>/restore")
def restore_prompt(module_id):
    body = request.get_json(force=True)
    with session_scope() as session:
        module = session.get(PromptModule, module_id)
        old = session.get(PromptVersion, body["version_id"])
        if not module or not old:
            return error_response("not found", 404, "not_found")
        new = prompt_service.restore_as_new_version(session, module, old)
        return envelope({"version_id": new.id, "version": new.version}, status=201)


# ----------------------------------------------------------------- hypotheses

@api.post("/hypotheses")
def create_hypothesis():
    body = request.get_json(force=True)
    parsed = HypothesisSchema.model_validate(body)
    with session_scope() as session:
        project = session.get(ResearchProject, body.get("project_id")) or \
            session.execute(select(ResearchProject)).scalars().first()
        h = Hypothesis(project_id=project.id, research_question=parsed.research_question,
                       statement=parsed.statement,
                       independent_variable=parsed.independent_variable,
                       expected_effect=parsed.expected_effect,
                       created_by=body.get("created_by", "human"))
        session.add(h)
        session.flush()
        return envelope({"id": h.id}, status=201)


@api.get("/hypotheses")
def list_hypotheses():
    with session_scope() as session:
        rows = session.execute(select(Hypothesis)).scalars().all()
        return envelope([{
            "id": h.id, "research_question": h.research_question,
            "statement": h.statement, "independent_variable": h.independent_variable,
            "status": h.status, "created_by": h.created_by} for h in rows])


@api.get("/hypotheses/<hypothesis_id>")
def get_hypothesis(hypothesis_id):
    with session_scope() as session:
        h = session.get(Hypothesis, hypothesis_id)
        if not h:
            return error_response("not found", 404, "not_found")
        return envelope({"id": h.id, "research_question": h.research_question,
                         "statement": h.statement, "status": h.status,
                         "independent_variable": h.independent_variable,
                         "expected_effect": h.expected_effect})


# ------------------------------------------------------------------- personas

@api.get("/personas")
def list_personas():
    with session_scope() as session:
        personas = PersonaRepository(session).list_personas()
        suites = {s.id: s.key for s in session.execute(select(ScenarioSuite)).scalars()}
        members = session.execute(select(ScenarioSuiteMember)).scalars().all()
        suite_by_pv = {}
        for m in members:
            suite_by_pv.setdefault(m.persona_version_id, []).append(suites.get(m.suite_id))
        for p in personas:
            p["suites"] = suite_by_pv.get(p["latest_version_id"], [])
        return envelope(personas)


@api.get("/personas/<persona_version_id>")
def get_persona(persona_version_id):
    """Public packet only. Hidden truth is NEVER exposed here."""
    with session_scope() as session:
        try:
            packet = PersonaRepository(session).get_public_packet(persona_version_id)
        except KeyError:
            return error_response("not found", 404, "not_found")
        return envelope({"persona_version_id": persona_version_id,
                         "public_packet": packet,
                         "note": "Hidden truth is not available on this endpoint."})


@api.post("/personas/<persona_version_id>/hidden-truth")
def owner_hidden_truth(persona_version_id):
    """Restricted local-owner view. Requires explicit acknowledgment that opening
    it may invalidate blind human review. Access is audited."""
    body = request.get_json(silent=True) or {}
    with session_scope() as session:
        try:
            truth = HiddenTruthRepository(session).get_truth(
                "local_owner", persona_version_id,
                acknowledge_blind_invalidations=bool(body.get("acknowledge", False)))
        except HiddenTruthAccessDenied as exc:
            return error_response(str(exc), 403, "hidden_truth_denied")
        except HoldoutLockedError as exc:
            return error_response(str(exc), 403, "holdout_locked")
        except KeyError:
            return error_response("not found", 404, "not_found")
        return envelope({"hidden_truth": truth,
                         "warning": "Viewing hidden truth may invalidate blind review. "
                                    "This access has been audited."})


@api.get("/scenario-suites")
def list_suites():
    with session_scope() as session:
        out = []
        for suite in session.execute(select(ScenarioSuite)).scalars():
            members = session.execute(select(ScenarioSuiteMember).where(
                ScenarioSuiteMember.suite_id == suite.id)).scalars().all()
            out.append({"id": suite.id, "key": suite.key, "name": suite.name,
                        "visibility": suite.visibility, "locked": suite.locked,
                        "member_persona_version_ids": [m.persona_version_id for m in members]})
        return envelope(out)


# ---------------------------------------------------------------- experiments

def _experiment_dict(e: Experiment):
    return {"id": e.id, "code": e.code, "name": e.name, "state": e.state,
            "spec": e.spec_json, "estimated_cost_usd": e.estimated_cost_usd,
            "actual_cost_usd": round(e.actual_cost_usd, 4),
            "result_label": e.result_label, "decision": e.decision_json,
            "is_template": e.is_template, "hypothesis_id": e.hypothesis_id,
            "cancel_requested": e.cancel_requested,
            "created_at": e.created_at.isoformat()}


@api.get("/experiments")
def list_experiments():
    with session_scope() as session:
        rows = session.execute(select(Experiment)
                               .order_by(Experiment.created_at.desc())).scalars().all()
        return envelope([_experiment_dict(e) for e in rows])


@api.post("/experiments")
def create_experiment_route():
    body = request.get_json(force=True)
    design = ExperimentDesignSchema.model_validate(body["design"])
    with session_scope() as session:
        project = session.get(ResearchProject, body.get("project_id")) or \
            session.execute(select(ResearchProject)).scalars().first()
        if not project:
            return error_response("create a project first", 422, "no_project")
        try:
            experiment = exp_service.create_experiment(
                session, project.id, body["name"], design,
                code=body.get("code", ""), hypothesis_id=body.get("hypothesis_id"))
        except ExperimentValidationError as exc:
            return error_response(str(exc), 422, "design_invalid")
        return envelope(_experiment_dict(experiment), status=201)


@api.post("/experiments/<experiment_id>/estimate")
def estimate_experiment(experiment_id):
    with session_scope() as session:
        e = session.get(Experiment, experiment_id)
        if not e:
            return error_response("not found", 404, "not_found")
        design = ExperimentDesignSchema.model_validate(e.spec_json)
        estimate = exp_service.estimate_cost(session, design)
        e.estimated_cost_usd = estimate["estimated_total_usd"]
        return envelope(estimate)


@api.post("/experiments/<experiment_id>/launch")
def launch_experiment(experiment_id):
    idem = request.headers.get("Idempotency-Key")
    with session_scope() as session:
        e = session.get(Experiment, experiment_id)
        if not e:
            return error_response("not found", 404, "not_found")
        if e.is_template:
            return error_response("Templates cannot be launched directly; clone first "
                                  "via POST /experiments with this spec.", 409, "template")
        try:
            result = exp_service.launch(session, e, idempotency_key=idem)
        except ExperimentValidationError as exc:
            return error_response(str(exc), 422, "design_invalid")
        except budget_service.BudgetExceededError as exc:
            return error_response(str(exc), 429, "budget_blocked")
        except Exception as exc:
            from state_machine import InvalidTransition
            if isinstance(exc, InvalidTransition):
                return error_response(str(exc), 409, "invalid_state")
            raise
        return envelope(result, status=202)


@api.post("/experiments/<experiment_id>/pause")
def pause_experiment(experiment_id):
    with session_scope() as session:
        e = session.get(Experiment, experiment_id)
        if not e:
            return error_response("not found", 404, "not_found")
        exp_service.request_pause(session, e)
        return envelope({"state": e.state, "pause_requested": True})


@api.post("/experiments/<experiment_id>/resume")
def resume_experiment(experiment_id):
    with session_scope() as session:
        e = session.get(Experiment, experiment_id)
        if not e:
            return error_response("not found", 404, "not_found")
        exp_service.resume(session, e)
        return envelope({"state": e.state}, status=202)


@api.post("/experiments/<experiment_id>/cancel")
def cancel_experiment(experiment_id):
    with session_scope() as session:
        e = session.get(Experiment, experiment_id)
        if not e:
            return error_response("not found", 404, "not_found")
        exp_service.request_cancel(session, e)
        return envelope({"state": e.state, "cancel_requested": True}, status=202)


@api.get("/experiments/<experiment_id>")
def get_experiment(experiment_id):
    with session_scope() as session:
        e = session.get(Experiment, experiment_id)
        if not e:
            return error_response("not found", 404, "not_found")
        data = _experiment_dict(e)
        data["arms"] = exp_service.architecture_label_map(session, experiment_id)
        scenarios = session.execute(select(ExperimentScenario).where(
            ExperimentScenario.experiment_id == experiment_id)).scalars().all()
        data["scenarios"] = [{"persona_version_id": s.persona_version_id,
                              "interview_mode": s.interview_mode,
                              "repetitions": s.repetitions} for s in scenarios]
        return envelope(data)


@api.get("/experiments/<experiment_id>/progress")
def experiment_progress(experiment_id):
    after_seq = int(request.args.get("after", 0))
    with session_scope() as session:
        rows = session.execute(select(ProgressEvent).where(
            ProgressEvent.subject_type == "experiment",
            ProgressEvent.subject_id == experiment_id,
            ProgressEvent.seq > after_seq).order_by(ProgressEvent.seq)).scalars().all()
        return envelope([{"seq": r.seq, "event_type": r.event_type,
                          "payload": r.payload_json,
                          "created_at": r.created_at.isoformat()} for r in rows])


@api.get("/experiments/<experiment_id>/events")
def experiment_events_sse(experiment_id):
    """Server-Sent Events stream of progress events."""
    cfg = get_config()

    def stream():
        last_seq = int(request.args.get("after", 0))
        last_beat = time.time()
        while True:
            with session_scope() as session:
                rows = session.execute(select(ProgressEvent).where(
                    ProgressEvent.subject_type == "experiment",
                    ProgressEvent.subject_id == experiment_id,
                    ProgressEvent.seq > last_seq)
                    .order_by(ProgressEvent.seq)).scalars().all()
                exp = session.get(Experiment, experiment_id)
                state = exp.state if exp else "UNKNOWN"
            for row in rows:
                last_seq = row.seq
                yield (f"event: {row.event_type}\n"
                       f"data: {json.dumps({'seq': row.seq, 'payload': row.payload_json, 'state': state})}\n\n")
            if state in ("COMPLETED", "CANCELLED", "FAILED", "BUDGET_BLOCKED",
                         "SAFETY_BLOCKED", "INCONCLUSIVE", "NEEDS_HUMAN_REVIEW"):
                yield f"event: done\ndata: {json.dumps({'state': state})}\n\n"
                return
            if time.time() - last_beat > cfg.sse_heartbeat_seconds:
                last_beat = time.time()
                yield ": heartbeat\n\n"
            time.sleep(1.0)

    return Response(stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@api.get("/experiments/<experiment_id>/runs")
def experiment_runs(experiment_id):
    with session_scope() as session:
        rows = session.execute(select(InterviewRun).where(
            InterviewRun.experiment_id == experiment_id)).scalars().all()
        return envelope([{
            "id": r.id, "architecture_version_id": r.architecture_version_id,
            "persona_version_id": r.persona_version_id, "mode": r.interview_mode,
            "repetition": r.repetition_index, "status": r.status,
            "termination_reason": r.termination_reason,
            "cost_usd": round(r.total_cost_usd, 5),
            "llm_calls": r.llm_call_count, "latency_ms": r.total_latency_ms}
            for r in rows])


@api.get("/experiments/<experiment_id>/comparison")
def experiment_comparison(experiment_id):
    with session_scope() as session:
        e = session.get(Experiment, experiment_id)
        if not e:
            return error_response("not found", 404, "not_found")
        pairwise = session.execute(select(PairwiseComparison).where(
            PairwiseComparison.experiment_id == experiment_id)).scalars().all()
        disagreements = session.execute(select(JudgeDisagreement).where(
            JudgeDisagreement.experiment_id == experiment_id)).scalars().all()
        critic = session.execute(select(CriticReview).where(
            CriticReview.experiment_id == experiment_id)).scalars().all()
        return envelope({
            "state": e.state, "result_label": e.result_label,
            "decision": e.decision_json,
            "arms": exp_service.architecture_label_map(session, experiment_id),
            "pairwise": [{"ordering": p.ordering, "output": p.judge_output_json}
                         for p in pairwise],
            "disagreements": [{"metric_key": d.metric_key, "detail": d.detail_json,
                               "status": d.status} for d in disagreements],
            "critic_reviews": [c.output_json for c in critic]})


# ----------------------------------------------------------------------- runs

@api.get("/runs/<run_id>")
def get_run(run_id):
    with session_scope() as session:
        r = session.get(InterviewRun, run_id)
        if not r:
            return error_response("not found", 404, "not_found")
        return envelope({
            "id": r.id, "experiment_id": r.experiment_id,
            "architecture_version_id": r.architecture_version_id,
            "persona_version_id": r.persona_version_id, "mode": r.interview_mode,
            "status": r.status, "termination_reason": r.termination_reason,
            "cost_usd": round(r.total_cost_usd, 5),
            "input_tokens": r.total_input_tokens, "output_tokens": r.total_output_tokens,
            "llm_calls": r.llm_call_count, "latency_ms": r.total_latency_ms,
            "provider_seed": r.provider_seed,
            "config_snapshot": r.config_snapshot_json,
            "final_report": r.final_report_json})


@api.get("/runs/<run_id>/transcript")
def get_transcript(run_id):
    """Transcript WITHOUT the protected simulator channel."""
    with session_scope() as session:
        turns = session.execute(select(InterviewTurn).where(
            InterviewTurn.interview_run_id == run_id)
            .order_by(InterviewTurn.turn_index)).scalars().all()
        return envelope([{
            "turn_index": t.turn_index, "interviewer": t.interviewer_text,
            "candidate": t.candidate_text,
            "meta": {k: v for k, v in (t.interviewer_meta_json or {}).items()},
            "latency_ms": t.latency_ms, "cost_usd": round(t.cost_usd, 5)}
            for t in turns])


@api.get("/runs/<run_id>/state")
def get_run_state(run_id):
    with session_scope() as session:
        snapshots = session.execute(select(InterviewStateSnapshot).where(
            InterviewStateSnapshot.interview_run_id == run_id)
            .order_by(InterviewStateSnapshot.turn_index)).scalars().all()
        return envelope([{"turn_index": s.turn_index, "state": s.state_json}
                         for s in snapshots])


@api.get("/runs/<run_id>/memory")
def get_run_memory(run_id):
    with session_scope() as session:
        snapshots = session.execute(select(MemorySnapshot).where(
            MemorySnapshot.interview_run_id == run_id)
            .order_by(MemorySnapshot.turn_index)).scalars().all()
        events = session.execute(select(MemoryEvent).where(
            MemoryEvent.interview_run_id == run_id)
            .order_by(MemoryEvent.turn_index)).scalars().all()
        return envelope({
            "snapshots": [{"turn_index": s.turn_index, "strategy": s.strategy,
                           "size_chars": s.size_chars, "memory": s.memory_json}
                          for s in snapshots],
            "events": [{"turn_index": e.turn_index, "operation": e.operation,
                        "detail": e.detail_json} for e in events]})


@api.get("/runs/<run_id>/costs")
def get_run_costs(run_id):
    with session_scope() as session:
        calls = session.execute(select(ModelCall).where(
            ModelCall.interview_run_id == run_id)).scalars().all()
        return envelope([{
            "agent_role": c.agent_role, "model": c.model,
            "provider_mode": c.provider_mode, "input_tokens": c.input_tokens,
            "output_tokens": c.output_tokens, "latency_ms": c.latency_ms,
            "cost_usd": round(c.cost_usd, 6), "status": c.status,
            "retry_number": c.retry_number} for c in calls])


@api.get("/runs/<run_id>/evaluations")
def get_run_evaluations(run_id):
    with session_scope() as session:
        evals = session.execute(select(EvaluationRun).where(
            EvaluationRun.interview_run_id == run_id)).scalars().all()
        scores = session.execute(select(MetricScore).where(
            MetricScore.interview_run_id == run_id)).scalars().all()
        det = session.execute(select(DeterministicMeasurement).where(
            DeterministicMeasurement.interview_run_id == run_id)).scalars().all()
        hard = session.execute(select(HardConstraintFinding).where(
            HardConstraintFinding.interview_run_id == run_id)).scalars().all()
        disagreements = session.execute(select(JudgeDisagreement).where(
            JudgeDisagreement.interview_run_id == run_id)).scalars().all()
        return envelope({
            "evaluators": [{"id": e.id, "role": e.evaluator_role,
                            "output": e.output_json, "cost_usd": e.cost_usd}
                           for e in evals],
            "metric_scores": [{"metric_key": s.metric_key, "score": s.score,
                               "confidence": s.confidence,
                               "evidence_turn_ids": s.evidence_turn_ids_json,
                               "reason": s.reason,
                               "evaluation_run_id": s.evaluation_run_id}
                              for s in scores],
            "deterministic": [{"metric_key": d.metric_key, "value": d.value,
                               "method": d.method, "limitations": d.limitations}
                              for d in det],
            "hard_constraints": [{"constraint_key": h.constraint_key,
                                  "severity": h.severity, "description": h.description,
                                  "resolved": h.resolved, "source": h.source}
                                 for h in hard],
            "disagreements": [{"metric_key": d.metric_key, "detail": d.detail_json,
                               "status": d.status} for d in disagreements]})


# ----------------------------------------------------------- research runs

@api.post("/projects/<project_id>/research-runs")
def start_research_run(project_id):
    body = request.get_json(silent=True) or {}
    cfg = get_config()
    with session_scope() as session:
        cfg_obj = get_config()
        try:
            cfg_obj.assert_isolation()
        except Exception as exc:
            return error_response(str(exc), 409, "isolation")
        rr = ResearchRun(project_id=project_id,
                         max_rounds=min(int(body.get("max_rounds", 3)),
                                        cfg.max_autonomous_rounds),
                         max_budget_usd=float(body.get("max_budget_usd", 5.0)))
        session.add(rr)
        session.flush()
        session.add(BackgroundJob(job_type="autonomous_cycle", subject_id=rr.id,
                                  payload_json={"research_run_id": rr.id}))
        return envelope({"research_run_id": rr.id}, status=202)


@api.get("/research-runs/<rr_id>")
def get_research_run(rr_id):
    with session_scope() as session:
        rr = session.get(ResearchRun, rr_id)
        if not rr:
            return error_response("not found", 404, "not_found")
        return envelope({"id": rr.id, "status": rr.status,
                         "rounds_completed": rr.rounds_completed,
                         "experiments_launched": rr.experiments_launched,
                         "max_rounds": rr.max_rounds,
                         "max_budget_usd": rr.max_budget_usd,
                         "stop_reason": rr.stop_reason, "log": rr.log_json})


@api.post("/research-runs/<rr_id>/pause")
def pause_research_run(rr_id):
    with session_scope() as session:
        rr = session.get(ResearchRun, rr_id)
        if not rr:
            return error_response("not found", 404, "not_found")
        rr.status = "paused"
        return envelope({"status": rr.status})


@api.post("/research-runs/<rr_id>/resume")
def resume_research_run(rr_id):
    with session_scope() as session:
        rr = session.get(ResearchRun, rr_id)
        if not rr:
            return error_response("not found", 404, "not_found")
        rr.status = "running"
        session.add(BackgroundJob(job_type="autonomous_cycle", subject_id=rr.id,
                                  payload_json={"research_run_id": rr.id}))
        return envelope({"status": rr.status}, status=202)


@api.post("/research-runs/<rr_id>/cancel")
def cancel_research_run(rr_id):
    with session_scope() as session:
        rr = session.get(ResearchRun, rr_id)
        if not rr:
            return error_response("not found", 404, "not_found")
        rr.status = "cancelled"
        return envelope({"status": rr.status})


# ------------------------------------------------- final validation / report

@api.post("/projects/<project_id>/final-validation")
def final_validation(project_id):
    with session_scope() as session:
        unlock_holdout_for_validation(session, project_id)
        return envelope({"holdout_unlocked": True,
                         "note": "Launch experiment E12 with holdout=true to run "
                                 "the blind validation."}, status=202)


@api.post("/projects/<project_id>/final-report")
def create_final_report(project_id):
    with session_scope() as session:
        rec = generate_final_recommendation(session, project_id)
        return envelope({"id": rec.id, "status": rec.status}, status=201)


@api.get("/projects/<project_id>/final-report")
def get_final_report(project_id):
    with session_scope() as session:
        rec = session.execute(select(FinalRecommendation).where(
            FinalRecommendation.project_id == project_id)
            .order_by(FinalRecommendation.created_at.desc())).scalars().first()
        if not rec:
            return error_response("no final report yet", 404, "not_found")
        return envelope({"id": rec.id, "status": rec.status, "report": rec.report_json,
                         "created_at": rec.created_at.isoformat()})


@api.get("/projects/<project_id>/timeline")
def get_timeline(project_id):
    with session_scope() as session:
        rows = session.execute(select(TimelineEvent).where(
            TimelineEvent.project_id == project_id)
            .order_by(TimelineEvent.created_at.desc()).limit(300)).scalars().all()
        return envelope([{"id": t.id, "event_type": t.event_type, "title": t.title,
                          "detail": t.detail_json, "actor": t.actor,
                          "created_at": t.created_at.isoformat()} for t in rows])


@api.get("/projects/<project_id>/pareto")
def get_pareto(project_id):
    from services.reporting import _architecture_quality_cost
    with session_scope() as session:
        points = _architecture_quality_cost(session, project_id)
        return envelope(pareto_frontier(points) if points else [])


@api.get("/projects/<project_id>/review-queue")
def review_queue(project_id):
    with session_scope() as session:
        rows = session.execute(select(HumanReviewItem).where(
            HumanReviewItem.project_id == project_id,
            HumanReviewItem.status == "open")).scalars().all()
        return envelope([{"id": r.id, "subject_type": r.subject_type,
                          "subject_id": r.subject_id, "reason": r.reason}
                         for r in rows])


@api.get("/workers")
def worker_status():
    with session_scope() as session:
        jobs = session.execute(select(BackgroundJob)
                               .order_by(BackgroundJob.created_at.desc())
                               .limit(50)).scalars().all()
        return envelope([{"id": j.id, "job_type": j.job_type, "status": j.status,
                          "subject_id": j.subject_id, "claimed_by": j.claimed_by,
                          "attempts": j.attempts, "error": j.error[:300],
                          "heartbeat_at": j.heartbeat_at.isoformat() if j.heartbeat_at else None}
                         for j in jobs])
