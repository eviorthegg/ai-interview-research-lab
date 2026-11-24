"""Architecture registry: versions, components, lineage, controlled mutations.

Rules enforced here:
- Version rows become immutable once used or promoted.
- The production baseline (is_protected) can never gain new versions from agents.
- Mutations: single component per normal experiment; bundled interventions must
  be labeled; duplicate mutations blocked via component hashes.
- Max 2 child variants per parent per autonomous round.
"""
from __future__ import annotations

import hashlib
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from models import (Architecture, ArchitectureStatusEvent, ArchitectureVersion,
                    TimelineEvent)
from schemas import MutationSchema

INTERVIEW_MODES = [
    "HR", "Technical", "HRJobDescription", "TechnicalJobDescription",
    "CompanySpecificHR", "CompanySpecificTechnical", "InterviewToCV",
]

COMPONENT_KEYS = [
    "base_system_prompt_version", "mode_prompt_versions", "role_analysis_strategy",
    "candidate_context_strategy", "job_description_strategy", "planning_strategy",
    "question_policy", "follow_up_policy", "probing_policy", "evidence_tracking",
    "memory_strategy", "transcript_access", "retrieval_strategy", "observer_enabled",
    "planner_enabled", "scoring_visibility", "termination_policy", "report_strategy",
]

MEMORY_STRATEGIES = [
    "stateless", "full_transcript", "rolling_window", "structured_summary",
    "event_based", "evior_candidate_memory_v2", "hybrid",
]

PROTECTED_COMPONENTS = {"safety_policy_version"}  # agents may never mutate these


class ArchitectureError(RuntimeError):
    pass


class ImmutableArchitectureError(ArchitectureError):
    pass


class DuplicateMutationError(ArchitectureError):
    pass


def configuration_hash(components: dict, model_config: dict, limits: dict,
                       prompt_versions: dict) -> str:
    payload = json.dumps({"c": components, "m": model_config, "l": limits,
                          "p": prompt_versions}, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def create_architecture(session: Session, name: str, code: str = "",
                        description: str = "", is_protected: bool = False,
                        creation_source: str = "human") -> Architecture:
    arch = Architecture(name=name, code=code, description=description,
                        is_protected=is_protected, creation_source=creation_source)
    session.add(arch)
    session.flush()
    return arch


def create_version(session: Session, architecture: Architecture, components: dict,
                   model_config: dict | None = None, limits: dict | None = None,
                   interview_modes: list[str] | None = None,
                   prompt_module_versions: dict | None = None,
                   parent_version_id: str | None = None,
                   hypothesis_id: str | None = None,
                   change_description: str = "", expected_benefit: str = "",
                   expected_risk: str = "", status: str = "draft",
                   actor: str = "human", immutable: bool = False) -> ArchitectureVersion:
    if architecture.is_protected and actor == "agent":
        raise ImmutableArchitectureError(
            f"Architecture '{architecture.name}' is protected; agents cannot create versions.")
    latest = session.execute(
        select(ArchitectureVersion)
        .where(ArchitectureVersion.architecture_id == architecture.id)
        .order_by(ArchitectureVersion.version.desc())).scalars().first()
    if architecture.is_protected and latest is not None:
        raise ImmutableArchitectureError(
            f"Architecture '{architecture.name}' is immutable after import. "
            "Create a descendant architecture instead.")
    version = ArchitectureVersion(
        architecture_id=architecture.id,
        version=(latest.version + 1) if latest else 1,
        parent_version_id=parent_version_id, status=status,
        hypothesis_id=hypothesis_id, change_description=change_description,
        expected_benefit=expected_benefit, expected_risk=expected_risk,
        components_json=components, model_config_json=model_config or {},
        limits_json=limits or {},
        interview_mode_support_json=interview_modes or list(INTERVIEW_MODES),
        prompt_module_versions_json=prompt_module_versions or {},
        configuration_hash=configuration_hash(
            components, model_config or {}, limits or {}, prompt_module_versions or {}),
        immutable=immutable)
    session.add(version)
    session.flush()
    return version


def set_status(session: Session, version: ArchitectureVersion, to_status: str,
               reason: str = "", actor: str = "human") -> None:
    valid = {"draft", "active", "promoted", "rejected", "retired", "safety_rejected"}
    if to_status not in valid:
        raise ArchitectureError(f"Invalid status '{to_status}'")
    event = ArchitectureStatusEvent(
        architecture_version_id=version.id, from_status=version.status,
        to_status=to_status, reason=reason, actor=actor)
    version.status = to_status
    if to_status in ("promoted", "rejected", "safety_rejected"):
        version.immutable = True
    session.add(event)
    session.flush()


def lineage(session: Session, version: ArchitectureVersion) -> list[dict]:
    """Ancestry chain, root first."""
    chain: list[dict] = []
    current: ArchitectureVersion | None = version
    guard = 0
    while current is not None and guard < 100:
        arch = session.get(Architecture, current.architecture_id)
        chain.append({
            "architecture_version_id": current.id, "architecture_id": current.architecture_id,
            "name": arch.name if arch else "?", "version": current.version,
            "status": current.status, "change_description": current.change_description,
            "configuration_hash": current.configuration_hash})
        current = (session.get(ArchitectureVersion, current.parent_version_id)
                   if current.parent_version_id else None)
        guard += 1
    chain.reverse()
    return chain


def component_diff(a: ArchitectureVersion, b: ArchitectureVersion) -> dict:
    diff = {}
    keys = set(a.components_json) | set(b.components_json)
    for key in sorted(keys):
        va, vb = a.components_json.get(key), b.components_json.get(key)
        if va != vb:
            diff[key] = {"a": va, "b": vb}
    return diff


def _mutation_hash(parent_version_id: str, target_component: str, proposed_after: dict) -> str:
    return hashlib.sha256(json.dumps(
        {"p": parent_version_id, "t": target_component, "a": proposed_after},
        sort_keys=True).encode()).hexdigest()


def apply_mutation(session: Session, mutation: MutationSchema, project_id: str,
                   actor: str = "agent", round_variant_count: int = 0,
                   max_variants_per_round: int = 2) -> ArchitectureVersion:
    """Validate and apply a controlled mutation, producing a child version."""
    parent = session.get(ArchitectureVersion, mutation.parent_architecture_id)
    if parent is None:
        raise ArchitectureError("Parent architecture version not found.")
    parent_arch = session.get(Architecture, parent.architecture_id)

    if mutation.target_component in PROTECTED_COMPONENTS:
        raise ArchitectureError(
            f"Component '{mutation.target_component}' is protected and cannot be mutated.")
    if mutation.mutation_scope == "single_component":
        changed_keys = set(mutation.proposed_after.keys())
        if len(changed_keys) != 1 or mutation.target_component not in changed_keys:
            raise ArchitectureError(
                "single_component mutations must change exactly the target component.")
    if actor == "agent" and round_variant_count >= max_variants_per_round:
        raise ArchitectureError(
            f"Maximum {max_variants_per_round} child variants per parent per autonomous round.")

    # Duplicate detection via component hashes
    mut_hash = _mutation_hash(parent.id, mutation.target_component, mutation.proposed_after)
    existing = session.execute(
        select(ArchitectureVersion)
        .where(ArchitectureVersion.parent_version_id == parent.id)).scalars().all()
    for sibling in existing:
        sib_hash = _mutation_hash(
            parent.id, mutation.target_component,
            {mutation.target_component: sibling.components_json.get(mutation.target_component)})
        if sib_hash == mut_hash:
            raise DuplicateMutationError(
                "A semantically identical mutation of this parent already exists.")

    components = dict(parent.components_json)
    components.update(mutation.proposed_after)

    # Descendant architectures of a protected baseline get their own architecture row
    if parent_arch.is_protected:
        child_arch = create_architecture(
            session, name=f"{parent_arch.name} :: {mutation.target_component} variant "
                          f"{mut_hash[:6]}",
            description=mutation.reason, creation_source=actor)
    else:
        child_arch = parent_arch

    child = create_version(
        session, child_arch, components,
        model_config=dict(parent.model_config_json),
        limits=dict(parent.limits_json),
        interview_modes=list(parent.interview_mode_support_json),
        prompt_module_versions=dict(parent.prompt_module_versions_json),
        parent_version_id=parent.id, hypothesis_id=mutation.hypothesis_id,
        change_description=f"[{mutation.mutation_scope}] {mutation.target_component}: "
                           f"{mutation.reason}",
        expected_benefit=json.dumps(mutation.expected_metric_changes),
        expected_risk="; ".join(mutation.risk_assessment),
        actor=actor, status="draft")
    session.add(TimelineEvent(
        project_id=project_id, event_type="architecture_mutation",
        title=f"Mutation: {mutation.target_component} on {parent_arch.name} v{parent.version}",
        detail_json={"mutation": json.loads(mutation.model_dump_json()),
                     "child_version_id": child.id},
        actor=actor))
    return child
