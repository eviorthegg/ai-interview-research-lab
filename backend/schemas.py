"""Pydantic 2 contracts for all structured agent handoffs.

Invalid agent outputs never continue silently:
services.provider.call_structured retries ONCE with the schema errors,
then fails the step, storing both responses.
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator

SCHEMA_VERSION = "1.0"


# ------------------------------------------------------------ research layer

class ResearchDecisionSchema(BaseModel):
    decision: Literal["run_experiment", "validate", "stop", "request_human_review"]
    research_question: str
    rationale: str
    candidate_parent_architectures: list[str] = Field(default_factory=list)
    target_components: list[str] = Field(default_factory=list)
    suggested_scenario_tags: list[str] = Field(default_factory=list)
    maximum_budget: float = 0.0
    stopping_reason: Optional[str] = None


class HypothesisSchema(BaseModel):
    research_question: str
    statement: str
    independent_variable: str
    expected_effect: str


class MutationSchema(BaseModel):
    parent_architecture_id: str
    hypothesis_id: str
    mutation_scope: Literal["single_component", "bundled_intervention"]
    target_component: str
    before_version: Optional[str] = None
    proposed_after: dict[str, Any]
    reason: str
    expected_metric_changes: list[dict] = Field(default_factory=list)
    risk_assessment: list[str] = Field(default_factory=list)
    requires_human_approval: bool = False


class ExperimentDesignSchema(BaseModel):
    research_question: str
    hypothesis_id: str
    control_architecture_version_id: str
    variant_architecture_version_id: str
    independent_variable: str
    dependent_metrics: list[str]
    controlled_variables: list[str] = Field(default_factory=list)
    scenario_persona_version_ids: list[str]
    interview_modes: list[str]
    repetitions: int = 2
    randomization_method: str = "fixed_pairing_random_blind_labels"
    max_cost_usd: float
    max_duration_seconds: int = 3600
    promotion_criteria: dict[str, Any] = Field(default_factory=dict)
    rejection_criteria: dict[str, Any] = Field(default_factory=dict)
    stopping_conditions: list[str] = Field(default_factory=list)
    holdout: bool = False
    evaluator_versions: dict[str, str] = Field(default_factory=dict)
    rubric_version_id: str = ""
    statistical_plan: str = "paired_bootstrap_ci"
    model_config_lock: dict[str, Any] = Field(default_factory=dict)

    @field_validator("repetitions")
    @classmethod
    def _reps(cls, v: int) -> int:
        if v < 1 or v > 10:
            raise ValueError("repetitions must be 1..10")
        return v


# --------------------------------------------------------- interview runtime

class SimulatorResponseSchema(BaseModel):
    spoken_answer: str
    used_truth_fact_ids: list[str] = Field(default_factory=list)
    used_exaggeration_ids: list[str] = Field(default_factory=list)
    response_style_tags: list[str] = Field(default_factory=list)
    simulator_consistency_warnings: list[str] = Field(default_factory=list)


class InterviewerTurnSchema(BaseModel):
    question: str
    reaction: str = ""
    internal_target: str = ""          # goal/competency this question serves (never spoken)
    is_follow_up: bool = False
    topic: str = ""
    end_interview: bool = False
    evidence_gap_targeted: str = ""


class EvidenceLedgerUpdateSchema(BaseModel):
    new_claims: list[dict] = Field(default_factory=list)
    new_evidence: list[dict] = Field(default_factory=list)
    new_gaps: list[str] = Field(default_factory=list)
    closed_gaps: list[str] = Field(default_factory=list)
    contradictions: list[dict] = Field(default_factory=list)


class MemoryDeltaSchema(BaseModel):
    """Delta proposed by LLM, merged by code (candidate_memory_v2 principle)."""
    facts: dict[str, Any] = Field(default_factory=dict)
    skills: list[dict] = Field(default_factory=list)
    strengths: list[str] = Field(default_factory=list)
    risks: list[dict] = Field(default_factory=list)
    contradictions: list[dict] = Field(default_factory=list)
    asked_questions: list[str] = Field(default_factory=list)
    coverage_updates: dict[str, float] = Field(default_factory=dict)
    remove: list[dict] = Field(default_factory=list)  # explicit removals with reason

    @field_validator("facts", mode="before")
    @classmethod
    def coerce_facts(cls, v: Any) -> Any:
        """Model sometimes sends facts as a list of strings — convert to dict."""
        if isinstance(v, list):
            return {f"fact_{i}": item for i, item in enumerate(v)}
        return v or {}

    @field_validator("risks", mode="before")
    @classmethod
    def coerce_risks(cls, v: Any) -> Any:
        """Model sometimes sends risks as a plain dict or a list of strings."""
        if isinstance(v, dict):
            return [{"risk": desc, "key": k} for k, desc in v.items()]
        if isinstance(v, list):
            return [{"risk": item} if isinstance(item, str) else item for item in v]
        return []

    @field_validator("coverage_updates", mode="before")
    @classmethod
    def coerce_coverage_updates(cls, v: Any) -> Any:
        """Model sometimes sends coverage_updates as an empty list []."""
        if isinstance(v, list):
            return {}
        return v or {}

    @field_validator("skills", "contradictions", "remove", mode="before")
    @classmethod
    def coerce_list_of_dicts(cls, v: Any) -> Any:
        """Ensure list fields don't come back as dicts or None."""
        if isinstance(v, dict):
            return [v]
        if v is None:
            return []
        return v



class ReportSchema(BaseModel):
    summary: str
    competency_findings: list[dict] = Field(default_factory=list)
    strengths: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    # every claim must carry evidence_turn_ids; enforced by evidence-link validation
    claims: list[dict] = Field(default_factory=list)
    recommendation: str = ""
    recommendation_basis: Literal["observed_evidence", "candidate_claims", "inference", "unknown"] = "observed_evidence"
    requires_human_review: bool = True


# ----------------------------------------------------------------- evaluation

class MetricScoreItem(BaseModel):
    metric_key: str
    score: float = Field(ge=0, le=100)
    confidence: float = Field(ge=0, le=1)
    evidence_turn_ids: list[str] = Field(default_factory=list)
    reason: str = ""
    failure_tags: list[str] = Field(default_factory=list)


class EvaluatorOutputSchema(BaseModel):
    scores: list[MetricScoreItem]
    hard_constraint_findings: list[dict] = Field(default_factory=list)
    overall_strengths: list[str] = Field(default_factory=list)
    overall_failures: list[str] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    requires_adjudication: bool = False


class PairwiseJudgeSchema(BaseModel):
    winner_by_group: dict[str, Literal["A", "B", "tie"]] = Field(default_factory=dict)
    overall_winner: Literal["A", "B", "tie"]
    evidence: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)
    hard_constraint_differences: list[str] = Field(default_factory=list)


class JudgeDecisionSchema(BaseModel):
    verdict: Literal["promote", "reject", "inconclusive", "needs_adjudication", "safety_reject"]
    rationale: str
    disagreements: list[dict] = Field(default_factory=list)


class CriticReviewSchema(BaseModel):
    causal_claim_issues: list[str] = Field(default_factory=list)
    leakage_concerns: list[str] = Field(default_factory=list)
    control_weaknesses: list[str] = Field(default_factory=list)
    overfitting_risks: list[str] = Field(default_factory=list)
    conflicting_metrics: list[str] = Field(default_factory=list)
    verdict: Literal["upheld", "challenged", "rejected"]
    rationale: str


class ImprovementProposalSchema(BaseModel):
    failure_pattern: str
    supporting_run_ids: list[str]
    proposed_mutation: MutationSchema
    justification: str


class FinalRecommendationSchema(BaseModel):
    status: Literal[
        "recommended_for_human_validation", "recommended_by_mode",
        "promising_but_unconfirmed", "inconclusive", "not_recommended", "safety_rejected"]
    overall_architecture_version_id: Optional[str] = None
    by_mode: dict[str, str] = Field(default_factory=dict)
    narrative: str = ""
    limitations: list[str] = Field(default_factory=list)
    next_experiments: list[str] = Field(default_factory=list)


SCHEMA_REGISTRY: dict[str, type[BaseModel]] = {
    "research_decision": ResearchDecisionSchema,
    "hypothesis": HypothesisSchema,
    "architecture_mutation": MutationSchema,
    "experiment_design": ExperimentDesignSchema,
    "candidate_simulator_response": SimulatorResponseSchema,
    "interview_turn": InterviewerTurnSchema,
    "evidence_ledger_update": EvidenceLedgerUpdateSchema,
    "memory_delta": MemoryDeltaSchema,
    "report": ReportSchema,
    "metric_evaluation": EvaluatorOutputSchema,
    "pairwise_comparison": PairwiseJudgeSchema,
    "judge_decision": JudgeDecisionSchema,
    "critic_review": CriticReviewSchema,
    "improvement_proposal": ImprovementProposalSchema,
    "final_recommendation": FinalRecommendationSchema,
}
