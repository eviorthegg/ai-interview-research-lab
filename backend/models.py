"""All database models for the Evior Interview Research Lab.

Normalized relational tables; JSON columns only for variable configuration.
Historical/version rows are immutable by convention and enforced in services.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (JSON, Boolean, DateTime, Float, ForeignKey, Index,
                        Integer, String, Text, UniqueConstraint)
from sqlalchemy.orm import Mapped, mapped_column

from db import Base, new_uuid, utcnow


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


def pk() -> Mapped[str]:
    return mapped_column(String(36), primary_key=True, default=new_uuid)


# --------------------------------------------------------------------- core

class ResearchProject(TimestampMixin, Base):
    __tablename__ = "research_projects"
    id: Mapped[str] = pk()
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    objective: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(40), default="active")  # active|paused|completed
    current_phase: Mapped[str] = mapped_column(String(60), default="baseline_verification")
    settings_json: Mapped[dict] = mapped_column(JSON, default=dict)
    is_seeded: Mapped[bool] = mapped_column(Boolean, default=False)


class ResearchPhase(TimestampMixin, Base):
    __tablename__ = "research_phases"
    id: Mapped[str] = pk()
    project_id: Mapped[str] = mapped_column(ForeignKey("research_projects.id"), index=True)
    key: Mapped[str] = mapped_column(String(60))  # baseline_verification|broad_exploration|...
    name: Mapped[str] = mapped_column(String(120))
    budget_usd: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(30), default="pending")
    order_index: Mapped[int] = mapped_column(Integer, default=0)


class Budget(TimestampMixin, Base):
    __tablename__ = "budgets"
    id: Mapped[str] = pk()
    project_id: Mapped[str] = mapped_column(ForeignKey("research_projects.id"), unique=True)
    total_usd: Mapped[float] = mapped_column(Float, nullable=False)
    spent_usd: Mapped[float] = mapped_column(Float, default=0.0)
    reserved_usd: Mapped[float] = mapped_column(Float, default=0.0)  # active reservations
    holdout_reserve_usd: Mapped[float] = mapped_column(Float, default=0.0)
    holdout_unlocked: Mapped[bool] = mapped_column(Boolean, default=False)
    version: Mapped[int] = mapped_column(Integer, default=0)  # optimistic locking


class BudgetReservation(TimestampMixin, Base):
    __tablename__ = "budget_reservations"
    id: Mapped[str] = pk()
    budget_id: Mapped[str] = mapped_column(ForeignKey("budgets.id"), index=True)
    scope_type: Mapped[str] = mapped_column(String(40))  # experiment|run|agent_call
    scope_id: Mapped[str] = mapped_column(String(36), index=True)
    amount_usd: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(20), default="active")  # active|released|consumed


class CostEvent(TimestampMixin, Base):
    __tablename__ = "cost_events"
    id: Mapped[str] = pk()
    project_id: Mapped[str] = mapped_column(ForeignKey("research_projects.id"), index=True)
    experiment_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    interview_run_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    agent_role: Mapped[str] = mapped_column(String(60), default="")
    model: Mapped[str] = mapped_column(String(80))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cached_tokens: Mapped[int] = mapped_column(Integer, default=0)
    estimated_usd: Mapped[float] = mapped_column(Float, default=0.0)
    actual_usd: Mapped[float] = mapped_column(Float, default=0.0)
    pricing_version_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(120), unique=True, nullable=True)


class ModelPricingVersion(TimestampMixin, Base):
    __tablename__ = "model_pricing_versions"
    id: Mapped[str] = pk()
    version: Mapped[int] = mapped_column(Integer)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    # {"gpt-5.6": {"input_per_m": 5.0, "output_per_m": 15.0, "cached_per_m": 2.5}, ...}
    pricing_json: Mapped[dict] = mapped_column(JSON, default=dict)


# --------------------------------------------------------- prompts / agents

class AgentDefinition(TimestampMixin, Base):
    __tablename__ = "agent_definitions"
    id: Mapped[str] = pk()
    role: Mapped[str] = mapped_column(String(60), unique=True)  # research_director|interviewer|...
    description: Mapped[str] = mapped_column(Text, default="")
    tool_allowlist_json: Mapped[list] = mapped_column(JSON, default=list)
    may_edit_protected_prompts: Mapped[bool] = mapped_column(Boolean, default=False)


class PromptModule(TimestampMixin, Base):
    __tablename__ = "prompt_modules"
    id: Mapped[str] = pk()
    key: Mapped[str] = mapped_column(String(120), unique=True)
    category: Mapped[str] = mapped_column(String(60))  # interviewer_identity|hr_policy|...
    description: Mapped[str] = mapped_column(Text, default="")
    is_protected: Mapped[bool] = mapped_column(Boolean, default=False)  # safety/isolation modules


class PromptVersion(TimestampMixin, Base):
    __tablename__ = "prompt_versions"
    __table_args__ = (UniqueConstraint("module_id", "version"),)
    id: Mapped[str] = pk()
    module_id: Mapped[str] = mapped_column(ForeignKey("prompt_modules.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="draft")  # draft|published
    template_text: Mapped[str] = mapped_column(Text)
    variables_json: Mapped[list] = mapped_column(JSON, default=list)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    parent_version_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_by: Mapped[str] = mapped_column(String(60), default="human")  # human|seed|agent


class Rubric(TimestampMixin, Base):
    __tablename__ = "rubrics"
    id: Mapped[str] = pk()
    key: Mapped[str] = mapped_column(String(80), unique=True)
    description: Mapped[str] = mapped_column(Text, default="")


class RubricVersion(TimestampMixin, Base):
    __tablename__ = "rubric_versions"
    __table_args__ = (UniqueConstraint("rubric_id", "version"),)
    id: Mapped[str] = pk()
    rubric_id: Mapped[str] = mapped_column(ForeignKey("rubrics.id"), index=True)
    version: Mapped[str] = mapped_column(String(20))
    metrics_json: Mapped[list] = mapped_column(JSON, default=list)  # list of metric definitions
    status: Mapped[str] = mapped_column(String(20), default="published")


# ------------------------------------------------------------ architectures

class Architecture(TimestampMixin, Base):
    __tablename__ = "architectures"
    id: Mapped[str] = pk()
    name: Mapped[str] = mapped_column(String(200), unique=True)
    code: Mapped[str] = mapped_column(String(20), default="")  # A0..A6 for seeds
    description: Mapped[str] = mapped_column(Text, default="")
    is_protected: Mapped[bool] = mapped_column(Boolean, default=False)  # production baseline
    creation_source: Mapped[str] = mapped_column(String(30), default="human")  # human|seeded|agent


class ArchitectureVersion(TimestampMixin, Base):
    __tablename__ = "architecture_versions"
    __table_args__ = (UniqueConstraint("architecture_id", "version"),)
    id: Mapped[str] = pk()
    architecture_id: Mapped[str] = mapped_column(ForeignKey("architectures.id"), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    parent_version_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(30), default="draft")
    # draft|active|promoted|rejected|retired|safety_rejected
    hypothesis_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    change_description: Mapped[str] = mapped_column(Text, default="")
    expected_benefit: Mapped[str] = mapped_column(Text, default="")
    expected_risk: Mapped[str] = mapped_column(Text, default="")
    components_json: Mapped[dict] = mapped_column(JSON, default=dict)
    model_config_json: Mapped[dict] = mapped_column(JSON, default=dict)
    limits_json: Mapped[dict] = mapped_column(JSON, default=dict)
    interview_mode_support_json: Mapped[list] = mapped_column(JSON, default=list)
    prompt_module_versions_json: Mapped[dict] = mapped_column(JSON, default=dict)  # {module_key: version_id}
    safety_policy_version_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    configuration_hash: Mapped[str] = mapped_column(String(64), index=True)
    immutable: Mapped[bool] = mapped_column(Boolean, default=False)


class ArchitectureStatusEvent(TimestampMixin, Base):
    __tablename__ = "architecture_status_events"
    id: Mapped[str] = pk()
    architecture_version_id: Mapped[str] = mapped_column(ForeignKey("architecture_versions.id"), index=True)
    from_status: Mapped[str] = mapped_column(String(30))
    to_status: Mapped[str] = mapped_column(String(30))
    reason: Mapped[str] = mapped_column(Text, default="")
    actor: Mapped[str] = mapped_column(String(60), default="human")


# ----------------------------------------------------------------- research

class Hypothesis(TimestampMixin, Base):
    __tablename__ = "hypotheses"
    id: Mapped[str] = pk()
    project_id: Mapped[str] = mapped_column(ForeignKey("research_projects.id"), index=True)
    research_question: Mapped[str] = mapped_column(Text)
    statement: Mapped[str] = mapped_column(Text)
    independent_variable: Mapped[str] = mapped_column(String(200), default="")
    expected_effect: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(30), default="open")  # open|supported|refuted|inconclusive
    created_by: Mapped[str] = mapped_column(String(60), default="human")


class Experiment(TimestampMixin, Base):
    __tablename__ = "experiments"
    id: Mapped[str] = pk()
    project_id: Mapped[str] = mapped_column(ForeignKey("research_projects.id"), index=True)
    hypothesis_id: Mapped[str | None] = mapped_column(ForeignKey("hypotheses.id"), nullable=True)
    code: Mapped[str] = mapped_column(String(20), default="")  # E1..E12 for seeds
    name: Mapped[str] = mapped_column(String(200))
    state: Mapped[str] = mapped_column(String(40), default="DRAFT", index=True)
    spec_json: Mapped[dict] = mapped_column(JSON, default=dict)  # full experiment specification
    estimated_cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    actual_cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    result_label: Mapped[str | None] = mapped_column(String(60), nullable=True)
    # exploratory|promising|confirmed_within_suite|inconclusive|rejected|safety_rejected
    decision_json: Mapped[dict] = mapped_column(JSON, default=dict)
    is_template: Mapped[bool] = mapped_column(Boolean, default=False)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    pause_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    research_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)


class ExperimentArchitecture(TimestampMixin, Base):
    __tablename__ = "experiment_architectures"
    id: Mapped[str] = pk()
    experiment_id: Mapped[str] = mapped_column(ForeignKey("experiments.id"), index=True)
    architecture_version_id: Mapped[str] = mapped_column(ForeignKey("architecture_versions.id"))
    arm: Mapped[str] = mapped_column(String(20))  # control|variant
    blind_label: Mapped[str] = mapped_column(String(4), default="")  # A|B assigned at launch


class ExperimentScenario(TimestampMixin, Base):
    __tablename__ = "experiment_scenarios"
    id: Mapped[str] = pk()
    experiment_id: Mapped[str] = mapped_column(ForeignKey("experiments.id"), index=True)
    persona_version_id: Mapped[str] = mapped_column(String(36))
    interview_mode: Mapped[str] = mapped_column(String(60))
    repetitions: Mapped[int] = mapped_column(Integer, default=2)


class RunStateEvent(TimestampMixin, Base):
    __tablename__ = "run_state_events"
    id: Mapped[str] = pk()
    experiment_id: Mapped[str] = mapped_column(ForeignKey("experiments.id"), index=True)
    from_state: Mapped[str] = mapped_column(String(40))
    to_state: Mapped[str] = mapped_column(String(40))
    reason: Mapped[str] = mapped_column(Text, default="")
    actor: Mapped[str] = mapped_column(String(60), default="system")


class ResearchDecision(TimestampMixin, Base):
    __tablename__ = "research_decisions"
    id: Mapped[str] = pk()
    project_id: Mapped[str] = mapped_column(ForeignKey("research_projects.id"), index=True)
    research_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    agent_role: Mapped[str] = mapped_column(String(60))
    decision_json: Mapped[dict] = mapped_column(JSON, default=dict)


class ResearchRun(TimestampMixin, Base):
    """A bounded autonomous research cycle."""
    __tablename__ = "research_runs"
    id: Mapped[str] = pk()
    project_id: Mapped[str] = mapped_column(ForeignKey("research_projects.id"), index=True)
    status: Mapped[str] = mapped_column(String(30), default="running")
    # running|paused|completed|cancelled|stopped_budget|stopped_safety|needs_human_review
    max_rounds: Mapped[int] = mapped_column(Integer, default=3)
    rounds_completed: Mapped[int] = mapped_column(Integer, default=0)
    experiments_launched: Mapped[int] = mapped_column(Integer, default=0)
    max_budget_usd: Mapped[float] = mapped_column(Float, default=10.0)
    stop_reason: Mapped[str] = mapped_column(Text, default="")
    log_json: Mapped[list] = mapped_column(JSON, default=list)


class TimelineEvent(TimestampMixin, Base):
    __tablename__ = "timeline_events"
    id: Mapped[str] = pk()
    project_id: Mapped[str] = mapped_column(ForeignKey("research_projects.id"), index=True)
    event_type: Mapped[str] = mapped_column(String(60), index=True)
    title: Mapped[str] = mapped_column(String(300))
    detail_json: Mapped[dict] = mapped_column(JSON, default=dict)
    actor: Mapped[str] = mapped_column(String(60), default="system")


# ------------------------------------------------------ personas / scenarios

class CandidatePersona(TimestampMixin, Base):
    __tablename__ = "candidate_personas"
    id: Mapped[str] = pk()
    key: Mapped[str] = mapped_column(String(80), unique=True)
    display_name: Mapped[str] = mapped_column(String(120))
    is_synthetic: Mapped[bool] = mapped_column(Boolean, default=True)


class CandidatePersonaVersion(TimestampMixin, Base):
    __tablename__ = "candidate_persona_versions"
    __table_args__ = (UniqueConstraint("persona_id", "version"),)
    id: Mapped[str] = pk()
    persona_id: Mapped[str] = mapped_column(ForeignKey("candidate_personas.id"), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    role_family: Mapped[str] = mapped_column(String(80))
    seniority: Mapped[str] = mapped_column(String(40))
    scenario_tags_json: Mapped[list] = mapped_column(JSON, default=list)
    seed: Mapped[int] = mapped_column(Integer, default=0)


class CandidatePublicPacket(TimestampMixin, Base):
    __tablename__ = "candidate_public_packets"
    id: Mapped[str] = pk()
    persona_version_id: Mapped[str] = mapped_column(ForeignKey("candidate_persona_versions.id"), unique=True)
    packet_json: Mapped[dict] = mapped_column(JSON, default=dict)


class CandidateHiddenTruth(TimestampMixin, Base):
    """PROTECTED. Access ONLY through repositories.HiddenTruthRepository."""
    __tablename__ = "candidate_hidden_truth"
    id: Mapped[str] = pk()
    persona_version_id: Mapped[str] = mapped_column(ForeignKey("candidate_persona_versions.id"), unique=True)
    truth_json: Mapped[dict] = mapped_column(JSON, default=dict)


class ScenarioSuite(TimestampMixin, Base):
    __tablename__ = "scenario_suites"
    id: Mapped[str] = pk()
    key: Mapped[str] = mapped_column(String(60), unique=True)  # development|regression|holdout
    name: Mapped[str] = mapped_column(String(120))
    visibility: Mapped[str] = mapped_column(String(30), default="development")
    # development (agents may see) | regression (ids only) | holdout (locked)
    locked: Mapped[bool] = mapped_column(Boolean, default=False)
    holdout_unlocked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ScenarioSuiteMember(TimestampMixin, Base):
    __tablename__ = "scenario_suite_members"
    __table_args__ = (UniqueConstraint("suite_id", "persona_version_id"),)
    id: Mapped[str] = pk()
    suite_id: Mapped[str] = mapped_column(ForeignKey("scenario_suites.id"), index=True)
    persona_version_id: Mapped[str] = mapped_column(ForeignKey("candidate_persona_versions.id"))


# ------------------------------------------------------------ interview data

class InterviewRun(TimestampMixin, Base):
    __tablename__ = "interview_runs"
    id: Mapped[str] = pk()
    experiment_id: Mapped[str | None] = mapped_column(ForeignKey("experiments.id"), index=True, nullable=True)
    architecture_version_id: Mapped[str] = mapped_column(String(36), index=True)
    persona_version_id: Mapped[str] = mapped_column(String(36), index=True)
    interview_mode: Mapped[str] = mapped_column(String(60))
    repetition_index: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    # pending|in_progress|completed|cancelled|failed|safety_stopped|budget_blocked
    termination_reason: Mapped[str | None] = mapped_column(String(80), nullable=True)
    state_json: Mapped[dict] = mapped_column(JSON, default=dict)  # interview runtime state
    total_cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    total_input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    llm_call_count: Mapped[int] = mapped_column(Integer, default=0)
    total_latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    provider_seed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    config_snapshot_json: Mapped[dict] = mapped_column(JSON, default=dict)  # reproducibility
    final_report_json: Mapped[dict] = mapped_column(JSON, default=dict)


class InterviewTurn(TimestampMixin, Base):
    __tablename__ = "interview_turns"
    __table_args__ = (Index("ix_turns_run_idx", "interview_run_id", "turn_index"),)
    id: Mapped[str] = pk()
    interview_run_id: Mapped[str] = mapped_column(ForeignKey("interview_runs.id"), index=True)
    turn_index: Mapped[int] = mapped_column(Integer)
    interviewer_text: Mapped[str] = mapped_column(Text, default="")
    candidate_text: Mapped[str] = mapped_column(Text, default="")
    interviewer_meta_json: Mapped[dict] = mapped_column(JSON, default=dict)
    # PROTECTED channel: simulator truth refs are NOT exposed on transcript endpoints
    simulator_protected_json: Mapped[dict] = mapped_column(JSON, default=dict)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)


class InterviewStateSnapshot(TimestampMixin, Base):
    __tablename__ = "interview_state_snapshots"
    id: Mapped[str] = pk()
    interview_run_id: Mapped[str] = mapped_column(ForeignKey("interview_runs.id"), index=True)
    turn_index: Mapped[int] = mapped_column(Integer)
    state_json: Mapped[dict] = mapped_column(JSON, default=dict)


class EvidenceRecord(TimestampMixin, Base):
    __tablename__ = "evidence_records"
    id: Mapped[str] = pk()
    interview_run_id: Mapped[str] = mapped_column(ForeignKey("interview_runs.id"), index=True)
    turn_index: Mapped[int] = mapped_column(Integer)
    claim: Mapped[str] = mapped_column(Text)
    evidence_type: Mapped[str] = mapped_column(String(40), default="other")
    competency: Mapped[str] = mapped_column(String(80), default="")
    strength: Mapped[str] = mapped_column(String(20), default="weak")  # weak|moderate|strong


class MemorySnapshot(TimestampMixin, Base):
    __tablename__ = "memory_snapshots"
    id: Mapped[str] = pk()
    interview_run_id: Mapped[str] = mapped_column(ForeignKey("interview_runs.id"), index=True)
    turn_index: Mapped[int] = mapped_column(Integer)
    strategy: Mapped[str] = mapped_column(String(60))
    memory_json: Mapped[dict] = mapped_column(JSON, default=dict)
    size_chars: Mapped[int] = mapped_column(Integer, default=0)


class MemoryEvent(TimestampMixin, Base):
    __tablename__ = "memory_events"
    id: Mapped[str] = pk()
    interview_run_id: Mapped[str] = mapped_column(ForeignKey("interview_runs.id"), index=True)
    turn_index: Mapped[int] = mapped_column(Integer)
    operation: Mapped[str] = mapped_column(String(30))  # read|write|summarize|delta_merge
    detail_json: Mapped[dict] = mapped_column(JSON, default=dict)


class ModelCall(TimestampMixin, Base):
    __tablename__ = "model_calls"
    id: Mapped[str] = pk()
    interview_run_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    experiment_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    agent_role: Mapped[str] = mapped_column(String(60))
    model: Mapped[str] = mapped_column(String(80))
    provider_mode: Mapped[str] = mapped_column(String(20), default="stub")
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    seed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    retry_number: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default="ok")  # ok|schema_error|provider_error


class RenderedPrompt(TimestampMixin, Base):
    __tablename__ = "rendered_prompts"
    id: Mapped[str] = pk()
    model_call_id: Mapped[str] = mapped_column(ForeignKey("model_calls.id"), index=True)
    agent_role: Mapped[str] = mapped_column(String(60))
    messages_json: Mapped[list] = mapped_column(JSON, default=list)  # fully rendered, secrets redacted


class Handoff(TimestampMixin, Base):
    __tablename__ = "handoffs"
    id: Mapped[str] = pk()
    run_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    experiment_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    from_agent: Mapped[str] = mapped_column(String(60))
    to_agent: Mapped[str] = mapped_column(String(60))
    schema_name: Mapped[str] = mapped_column(String(80))
    schema_version: Mapped[str] = mapped_column(String(20), default="1.0")
    payload_json: Mapped[dict] = mapped_column(JSON, default=dict)
    validation_status: Mapped[str] = mapped_column(String(20), default="valid")
    validation_errors_json: Mapped[list] = mapped_column(JSON, default=list)
    retry_number: Mapped[int] = mapped_column(Integer, default=0)


# ---------------------------------------------------------------- evaluation

class DeterministicMeasurement(TimestampMixin, Base):
    __tablename__ = "deterministic_measurements"
    id: Mapped[str] = pk()
    interview_run_id: Mapped[str] = mapped_column(ForeignKey("interview_runs.id"), index=True)
    metric_key: Mapped[str] = mapped_column(String(80), index=True)
    value: Mapped[float] = mapped_column(Float)
    method: Mapped[str] = mapped_column(String(60), default="deterministic")
    limitations: Mapped[str] = mapped_column(Text, default="")
    detail_json: Mapped[dict] = mapped_column(JSON, default=dict)


class EvaluationRun(TimestampMixin, Base):
    __tablename__ = "evaluation_runs"
    id: Mapped[str] = pk()
    interview_run_id: Mapped[str] = mapped_column(ForeignKey("interview_runs.id"), index=True)
    evaluator_role: Mapped[str] = mapped_column(String(60))  # core_qa|mode_specialist|fairness
    evaluator_version_id: Mapped[str] = mapped_column(String(36), default="")
    rubric_version_id: Mapped[str] = mapped_column(String(36), default="")
    status: Mapped[str] = mapped_column(String(20), default="completed")
    output_json: Mapped[dict] = mapped_column(JSON, default=dict)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)


class MetricScore(TimestampMixin, Base):
    __tablename__ = "metric_scores"
    __table_args__ = (Index("ix_scores_run_metric", "interview_run_id", "metric_key"),)
    id: Mapped[str] = pk()
    evaluation_run_id: Mapped[str] = mapped_column(ForeignKey("evaluation_runs.id"), index=True)
    interview_run_id: Mapped[str] = mapped_column(String(36), index=True)
    metric_key: Mapped[str] = mapped_column(String(80))
    score: Mapped[float] = mapped_column(Float)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    evidence_turn_ids_json: Mapped[list] = mapped_column(JSON, default=list)
    reason: Mapped[str] = mapped_column(Text, default="")
    failure_tags_json: Mapped[list] = mapped_column(JSON, default=list)


class HardConstraintFinding(TimestampMixin, Base):
    __tablename__ = "hard_constraint_findings"
    id: Mapped[str] = pk()
    interview_run_id: Mapped[str] = mapped_column(String(36), index=True)
    evaluation_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    constraint_key: Mapped[str] = mapped_column(String(80))
    severity: Mapped[str] = mapped_column(String(20), default="critical")
    description: Mapped[str] = mapped_column(Text, default="")
    evidence_turn_ids_json: Mapped[list] = mapped_column(JSON, default=list)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False)
    source: Mapped[str] = mapped_column(String(30), default="deterministic")  # deterministic|llm|human


class PairwiseComparison(TimestampMixin, Base):
    __tablename__ = "pairwise_comparisons"
    id: Mapped[str] = pk()
    experiment_id: Mapped[str] = mapped_column(String(36), index=True)
    run_a_id: Mapped[str] = mapped_column(String(36))
    run_b_id: Mapped[str] = mapped_column(String(36))
    ordering: Mapped[str] = mapped_column(String(10), default="ab")  # ab|ba
    judge_output_json: Mapped[dict] = mapped_column(JSON, default=dict)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)


class JudgeDisagreement(TimestampMixin, Base):
    __tablename__ = "judge_disagreements"
    id: Mapped[str] = pk()
    interview_run_id: Mapped[str] = mapped_column(String(36), index=True)
    experiment_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    metric_key: Mapped[str] = mapped_column(String(80))
    detail_json: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(30), default="open")  # open|adjudicated|human_review


class CriticReview(TimestampMixin, Base):
    __tablename__ = "critic_reviews"
    id: Mapped[str] = pk()
    experiment_id: Mapped[str] = mapped_column(String(36), index=True)
    output_json: Mapped[dict] = mapped_column(JSON, default=dict)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)


class HumanReviewItem(TimestampMixin, Base):
    __tablename__ = "human_review_items"
    id: Mapped[str] = pk()
    project_id: Mapped[str] = mapped_column(String(36), index=True)
    subject_type: Mapped[str] = mapped_column(String(40))
    subject_id: Mapped[str] = mapped_column(String(36))
    reason: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="open")  # open|resolved
    resolution: Mapped[str] = mapped_column(Text, default="")


# ---------------------------------------------------------------- operations

class BackgroundJob(TimestampMixin, Base):
    __tablename__ = "background_jobs"
    id: Mapped[str] = pk()
    job_type: Mapped[str] = mapped_column(String(60), index=True)  # run_experiment|autonomous_cycle|final_validation
    subject_id: Mapped[str] = mapped_column(String(36), index=True)
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    # queued|claimed|running|completed|failed|cancelled
    claimed_by: Mapped[str | None] = mapped_column(String(80), nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(Text, default="")
    payload_json: Mapped[dict] = mapped_column(JSON, default=dict)


class ProgressEvent(TimestampMixin, Base):
    __tablename__ = "progress_events"
    __table_args__ = (Index("ix_progress_subject", "subject_type", "subject_id", "seq"),)
    id: Mapped[str] = pk()
    subject_type: Mapped[str] = mapped_column(String(40))  # experiment|research_run
    subject_id: Mapped[str] = mapped_column(String(36))
    seq: Mapped[int] = mapped_column(Integer, default=0)
    event_type: Mapped[str] = mapped_column(String(60))
    payload_json: Mapped[dict] = mapped_column(JSON, default=dict)


class ErrorRecord(TimestampMixin, Base):
    __tablename__ = "errors"
    id: Mapped[str] = pk()
    scope_type: Mapped[str] = mapped_column(String(40), default="")
    scope_id: Mapped[str] = mapped_column(String(36), default="")
    error_type: Mapped[str] = mapped_column(String(120))
    message: Mapped[str] = mapped_column(Text)
    traceback: Mapped[str] = mapped_column(Text, default="")


class AuditEvent(TimestampMixin, Base):
    __tablename__ = "audit_events"
    id: Mapped[str] = pk()
    actor: Mapped[str] = mapped_column(String(80), default="system")
    action: Mapped[str] = mapped_column(String(120), index=True)
    subject_type: Mapped[str] = mapped_column(String(40), default="")
    subject_id: Mapped[str] = mapped_column(String(36), default="")
    detail_json: Mapped[dict] = mapped_column(JSON, default=dict)


class IdempotencyKey(TimestampMixin, Base):
    __tablename__ = "idempotency_keys"
    id: Mapped[str] = pk()
    key: Mapped[str] = mapped_column(String(120), unique=True)
    scope: Mapped[str] = mapped_column(String(60))
    response_json: Mapped[dict] = mapped_column(JSON, default=dict)


# -------------------------------------------------------------- final output

class FinalRecommendation(TimestampMixin, Base):
    __tablename__ = "final_recommendations"
    id: Mapped[str] = pk()
    project_id: Mapped[str] = mapped_column(String(36), index=True)
    status: Mapped[str] = mapped_column(String(60))
    # recommended_for_human_validation|recommended_by_mode|promising_but_unconfirmed|
    # inconclusive|not_recommended|safety_rejected
    report_json: Mapped[dict] = mapped_column(JSON, default=dict)


class ResearchReport(TimestampMixin, Base):
    __tablename__ = "research_reports"
    id: Mapped[str] = pk()
    project_id: Mapped[str] = mapped_column(String(36), index=True)
    report_type: Mapped[str] = mapped_column(String(40), default="final")
    content_json: Mapped[dict] = mapped_column(JSON, default=dict)
