"""Repositories, including the guarded hidden-truth access layer.

Isolation is enforced IN CODE: hidden truth is only reachable through
HiddenTruthRepository, which authorizes by caller role. The interviewer,
planner, observer, architecture designer and prompt generator code paths have
no accessor and any attempt raises HiddenTruthAccessDenied and is audited.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from models import (AuditEvent, CandidateHiddenTruth, CandidatePersona,
                    CandidatePersonaVersion, CandidatePublicPacket,
                    ScenarioSuite, ScenarioSuiteMember)

# Roles allowed to read hidden truth, and under which conditions.
_SIMULATOR_ROLES = {"candidate_simulator"}
_POST_INTERVIEW_ROLES = {"ground_truth_evaluator", "aggregator"}
_OWNER_ROLES = {"local_owner"}

FORBIDDEN_ROLES = {
    "interviewer", "planner", "observer", "architecture_designer",
    "prompt_generator", "research_director", "improvement_agent",
    "experiment_designer", "core_qa_evaluator", "mode_specialist_evaluator",
    "pairwise_judge",
}


class HiddenTruthAccessDenied(PermissionError):
    pass


class HoldoutLockedError(PermissionError):
    pass


class HiddenTruthRepository:
    """The ONLY sanctioned access path to candidate_hidden_truth rows."""

    def __init__(self, session: Session):
        self._session = session

    def _audit(self, role: str, persona_version_id: str, granted: bool, reason: str) -> None:
        self._session.add(AuditEvent(
            actor=role, action="hidden_truth_access",
            subject_type="candidate_persona_version", subject_id=persona_version_id,
            detail_json={"granted": granted, "reason": reason}))
        self._session.flush()

    def _holdout_locked(self, persona_version_id: str) -> bool:
        rows = self._session.execute(
            select(ScenarioSuite).join(ScenarioSuiteMember,
                                       ScenarioSuiteMember.suite_id == ScenarioSuite.id)
            .where(ScenarioSuiteMember.persona_version_id == persona_version_id)).scalars().all()
        return any(s.key == "holdout" and s.locked for s in rows)

    def get_truth(self, role: str, persona_version_id: str,
                  interview_completed: bool = False,
                  acknowledge_blind_invalidations: bool = False) -> dict:
        if role in FORBIDDEN_ROLES:
            self._audit(role, persona_version_id, False, "forbidden role")
            raise HiddenTruthAccessDenied(
                f"Role '{role}' is never allowed to read hidden truth.")
        if role in _SIMULATOR_ROLES:
            pass  # simulator runtime always allowed (it plays the persona)
        elif role in _POST_INTERVIEW_ROLES:
            if not interview_completed:
                self._audit(role, persona_version_id, False, "interview not completed")
                raise HiddenTruthAccessDenied(
                    "Evaluators may read hidden truth only after the interview completes.")
            if self._holdout_locked(persona_version_id):
                self._audit(role, persona_version_id, False, "holdout locked")
                raise HoldoutLockedError(
                    "Holdout persona truth is locked until final validation begins.")
        elif role in _OWNER_ROLES:
            if not acknowledge_blind_invalidations:
                self._audit(role, persona_version_id, False, "owner did not acknowledge")
                raise HiddenTruthAccessDenied(
                    "Owner access requires acknowledging that viewing hidden truth "
                    "may invalidate blind human review.")
        else:
            self._audit(role, persona_version_id, False, "unknown role")
            raise HiddenTruthAccessDenied(f"Unknown role '{role}'.")
        row = self._session.execute(
            select(CandidateHiddenTruth)
            .where(CandidateHiddenTruth.persona_version_id == persona_version_id)
        ).scalars().first()
        if row is None:
            raise KeyError(f"No hidden truth for persona version {persona_version_id}")
        self._audit(role, persona_version_id, True, "granted")
        return row.truth_json


class PersonaRepository:
    """Public persona data — safe for any consumer."""

    def __init__(self, session: Session):
        self._session = session

    def get_public_packet(self, persona_version_id: str) -> dict:
        row = self._session.execute(
            select(CandidatePublicPacket)
            .where(CandidatePublicPacket.persona_version_id == persona_version_id)
        ).scalars().first()
        if row is None:
            raise KeyError(f"No public packet for persona version {persona_version_id}")
        return row.packet_json

    def list_personas(self) -> list[dict]:
        out = []
        for persona in self._session.execute(select(CandidatePersona)).scalars():
            versions = self._session.execute(
                select(CandidatePersonaVersion)
                .where(CandidatePersonaVersion.persona_id == persona.id)
                .order_by(CandidatePersonaVersion.version.desc())).scalars().all()
            latest = versions[0] if versions else None
            out.append({
                "id": persona.id, "key": persona.key, "display_name": persona.display_name,
                "is_synthetic": persona.is_synthetic,
                "latest_version_id": latest.id if latest else None,
                "role_family": latest.role_family if latest else None,
                "seniority": latest.seniority if latest else None,
                "scenario_tags": latest.scenario_tags_json if latest else [],
                "seed": latest.seed if latest else None,
            })
        return out
