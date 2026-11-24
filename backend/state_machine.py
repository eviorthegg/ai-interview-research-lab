"""Experiment state machine. Every transition is stored as an immutable event."""
from __future__ import annotations

from sqlalchemy.orm import Session

from models import Experiment, ProgressEvent, RunStateEvent

STATES = [
    "DRAFT", "VALIDATING_CONFIGURATION", "ESTIMATING_COST", "QUEUED", "PREPARING",
    "RUNNING_INTERVIEWS", "RUNNING_DETERMINISTIC_ANALYSIS", "RUNNING_EVALUATORS",
    "AGGREGATING", "CRITIC_REVIEW", "DECIDING", "COMPLETED",
    "PAUSED", "CANCEL_REQUESTED", "CANCELLED", "FAILED", "BUDGET_BLOCKED",
    "SAFETY_BLOCKED", "NEEDS_HUMAN_REVIEW", "INCONCLUSIVE",
]

_PIPELINE = ["DRAFT", "VALIDATING_CONFIGURATION", "ESTIMATING_COST", "QUEUED",
             "PREPARING", "RUNNING_INTERVIEWS", "RUNNING_DETERMINISTIC_ANALYSIS",
             "RUNNING_EVALUATORS", "AGGREGATING", "CRITIC_REVIEW", "DECIDING",
             "COMPLETED"]

TERMINAL = {"COMPLETED", "CANCELLED", "FAILED", "BUDGET_BLOCKED", "SAFETY_BLOCKED",
            "INCONCLUSIVE"}

_EXTRA_ALLOWED: dict[str, set[str]] = {
    # from any running state you can pause / request cancel / fail / block
    **{s: {"PAUSED", "CANCEL_REQUESTED", "FAILED", "BUDGET_BLOCKED", "SAFETY_BLOCKED",
           "NEEDS_HUMAN_REVIEW"}
       for s in _PIPELINE if s not in ("DRAFT", "COMPLETED")},
    "PAUSED": set(_PIPELINE) | {"CANCEL_REQUESTED", "CANCELLED"},
    "CANCEL_REQUESTED": {"CANCELLED"},
    "NEEDS_HUMAN_REVIEW": {"DECIDING", "CANCELLED", "COMPLETED", "INCONCLUSIVE"},
    "DECIDING": {"COMPLETED", "INCONCLUSIVE", "NEEDS_HUMAN_REVIEW"},
    "DRAFT": {"VALIDATING_CONFIGURATION", "ESTIMATING_COST"},
}


class InvalidTransition(RuntimeError):
    pass


def allowed_transitions(state: str) -> set[str]:
    allowed = set(_EXTRA_ALLOWED.get(state, set()))
    if state in _PIPELINE:
        idx = _PIPELINE.index(state)
        if idx + 1 < len(_PIPELINE):
            allowed.add(_PIPELINE[idx + 1])
    return allowed


def transition(session: Session, experiment: Experiment, to_state: str,
               reason: str = "", actor: str = "system") -> None:
    if to_state not in STATES:
        raise InvalidTransition(f"Unknown state '{to_state}'")
    if experiment.state in TERMINAL:
        raise InvalidTransition(
            f"Experiment {experiment.id} is terminal ({experiment.state}).")
    if to_state not in allowed_transitions(experiment.state):
        raise InvalidTransition(
            f"Cannot transition {experiment.state} -> {to_state}")
    session.add(RunStateEvent(
        experiment_id=experiment.id, from_state=experiment.state,
        to_state=to_state, reason=reason, actor=actor))
    experiment.state = to_state
    emit_progress(session, "experiment", experiment.id, "state_change",
                  {"from": experiment.state, "to": to_state, "reason": reason})
    session.flush()


_seq_cache: dict[str, int] = {}


def emit_progress(session: Session, subject_type: str, subject_id: str,
                  event_type: str, payload: dict | None = None) -> None:
    key = f"{subject_type}:{subject_id}"
    seq = _seq_cache.get(key, 0) + 1
    _seq_cache[key] = seq
    session.add(ProgressEvent(subject_type=subject_type, subject_id=subject_id,
                              seq=seq, event_type=event_type,
                              payload_json=payload or {}))
    session.flush()
