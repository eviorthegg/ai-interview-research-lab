"""API endpoints for pipeline tournaments (separate blueprint)."""
from __future__ import annotations

from flask import Blueprint, request
from sqlalchemy import select

from api import envelope, error_response
from config import get_config
from db import session_scope
from models import (Architecture, ArchitectureVersion, BackgroundJob,
                    Experiment, ResearchRun)

pipelines_api = Blueprint("pipelines_api", __name__,
                          url_prefix="/api/v1/research-lab")


@pipelines_api.post("/projects/<project_id>/pipeline-tournaments")
def start_tournament(project_id):
    body = request.get_json(silent=True) or {}
    cfg = get_config()
    try:
        cfg.assert_isolation()
    except Exception as exc:
        return error_response(str(exc), 409, "isolation")
    params = {
        "num_pipelines": max(2, min(int(body.get("num_pipelines", 3)), 5)),
        "improve_rounds": max(0, min(int(body.get("improve_rounds", 1)), 4)),
        "interview_mode": body.get("interview_mode", "HR"),
        "allow_web": bool(body.get("allow_web", True)),
        "kind": "pipeline_tournament",
    }
    with session_scope() as session:
        run = ResearchRun(
            project_id=project_id,
            max_rounds=params["improve_rounds"],
            max_budget_usd=float(body.get("max_budget_usd", 3.0)),
            log_json=[params])
        session.add(run)
        session.flush()
        session.add(BackgroundJob(job_type="pipeline_tournament",
                                  subject_id=run.id,
                                  payload_json={"research_run_id": run.id}))
        return envelope({"tournament_id": run.id, "params": params}, status=202)


@pipelines_api.get("/pipeline-tournaments/<rr_id>")
def get_tournament(rr_id):
    with session_scope() as session:
        run = session.get(ResearchRun, rr_id)
        if not run:
            return error_response("not found", 404, "not_found")
        experiments = session.execute(select(Experiment).where(
            Experiment.research_run_id == rr_id)
            .order_by(Experiment.created_at)).scalars().all()
        spent = round(sum(e.actual_cost_usd for e in experiments), 4)
        return envelope({
            "id": run.id, "status": run.status, "stop_reason": run.stop_reason,
            "max_budget_usd": run.max_budget_usd, "spent_usd": spent,
            "experiments_launched": run.experiments_launched,
            "log": run.log_json,
            "experiments": [{
                "id": e.id, "name": e.name, "state": e.state,
                "result_label": e.result_label,
                "actual_cost_usd": round(e.actual_cost_usd, 4)}
                for e in experiments]})


@pipelines_api.get("/pipeline-tournaments")
def list_tournaments():
    with session_scope() as session:
        runs = session.execute(select(ResearchRun)
                               .order_by(ResearchRun.created_at.desc())).scalars().all()
        out = []
        for run in runs:
            log = run.log_json or []
            if not (log and isinstance(log[0], dict)
                    and log[0].get("kind") == "pipeline_tournament"):
                continue
            out.append({"id": run.id, "status": run.status,
                        "stop_reason": run.stop_reason,
                        "max_budget_usd": run.max_budget_usd,
                        "experiments_launched": run.experiments_launched,
                        "created_at": run.created_at.isoformat()})
        return envelope(out)


@pipelines_api.get("/pipelines")
def list_pipelines():
    """All pipeline architectures with their stage layout."""
    with session_scope() as session:
        out = []
        for arch in session.execute(select(Architecture).where(
                Architecture.name.like("[pipeline]%"))).scalars():
            versions = session.execute(select(ArchitectureVersion).where(
                ArchitectureVersion.architecture_id == arch.id)
                .order_by(ArchitectureVersion.version.desc())).scalars().all()
            for v in versions:
                spec = (v.components_json or {}).get("pipeline_stages") or {}
                out.append({
                    "architecture_version_id": v.id, "name": arch.name,
                    "version": v.version, "status": v.status,
                    "description": arch.description,
                    "pre_stages": [s.get("stage_type")
                                   for s in spec.get("pre_stages", [])],
                    "per_turn_stages": [s.get("stage_type")
                                        for s in spec.get("per_turn_stages", [])],
                    "spec": spec})
        return envelope(out)
