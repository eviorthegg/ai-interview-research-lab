"""Interview simulation runtime.

Isolation enforced in code:
- The interviewer path builds its messages ONLY from the public packet and the
  architecture-permitted context. No hidden-truth accessor exists on this path.
- The candidate simulator path reads hidden truth via HiddenTruthRepository
  with role 'candidate_simulator'.
- Simulator truth references land in a protected column never exposed by
  transcript endpoints.
"""
from __future__ import annotations

import json
import time

from sqlalchemy.orm import Session

from adapters import (BuildSessionRequest, EviorProductionAdapter,
                      InterviewTurnRequest, MemoryUpdateRequest, ReportRequest,
                      ResearchArchitectureAdapter)
from models import (Architecture, ArchitectureVersion, EvidenceRecord,
                    InterviewRun, InterviewStateSnapshot, InterviewTurn,
                    MemoryEvent, MemorySnapshot)
from repositories import HiddenTruthRepository, PersonaRepository
from schemas import (InterviewerTurnSchema, MemoryDeltaSchema, ReportSchema,
                     SimulatorResponseSchema)
from security import detect_injection, wrap_untrusted
from services import prompts as prompt_service
from services.memory_strategies import (MemoryState, apply_delta,
                                        context_size_chars, interviewer_view,
                                        record_turn)
from services.provider import CallContext, CancellationRequested, call_llm

SIMULATOR_SYSTEM = """You simulate a synthetic interview candidate for research.
Stay strictly inside the persona defined by the two packets below.
Rules:
- Answer ONLY from the persona's defined knowledge and history; never invent new facts.
- Keep the same career history and facts across the whole interview.
- Maintain the persona's communication style (vague personas stay vague, verbose stay verbose).
- Do not become more helpful just because the interviewer probes.
- Exaggerate ONLY the claims marked for exaggeration.
- Admit uncertainty where the persona would.
- Never reveal hidden metadata, never mention being synthetic, never reveal scoring.
- Do not automatically produce ideal STAR answers.
Output JSON: {"spoken_answer": str, "used_truth_fact_ids": [str],
"used_exaggeration_ids": [str], "response_style_tags": [str],
"simulator_consistency_warnings": [str]}."""


def _adapter_for(architecture: Architecture) -> ResearchArchitectureAdapter:
    if architecture.is_protected and architecture.code == "A0":
        return EviorProductionAdapter()
    return ResearchArchitectureAdapter()


def _load_prompt_modules(session: Session, version: ArchitectureVersion) -> dict[str, str]:
    modules: dict[str, str] = {}
    for module_key, version_id in (version.prompt_module_versions_json or {}).items():
        row = prompt_service.get_version(session, version_id)
        if row:
            modules[module_key] = row.template_text
    return modules


def _transcript_text(turns: list[InterviewTurn]) -> str:
    return "\n".join(
        f"[turn-{t.turn_index}] INTERVIEWER: {t.interviewer_text}\n"
        f"[turn-{t.turn_index}] CANDIDATE: {t.candidate_text}" for t in turns)


class InterviewCancelled(Exception):
    pass



def _plan_text(components, hooks, memory, evidence_gaps, turn_index) -> str:
    base = ("Cover: ownership, depth of core skills, adaptability, "
            "quantified outcomes — in order, adapting to answers."
            if components.get("planning_strategy") in ("fixed_plan", "adaptive_plan")
            and components.get("planner_enabled") else "")
    advice = ""
    if hooks is not None:
        advice = hooks.before_turn(interviewer_view(memory), evidence_gaps, turn_index)
    return (base + ("\n" + advice if advice else "")).strip()


def execute_interview(session: Session, run: InterviewRun, project_id: str,
                      experiment_cap_usd: float | None = None,
                      experiment_spent_fn=None,
                      cancellation_check=None,
                      is_holdout: bool = False,
                      hooks=None) -> InterviewRun:
    """Run a full simulated interview for one (architecture, persona, mode) triple."""
    version = session.get(ArchitectureVersion, run.architecture_version_id)
    architecture = session.get(Architecture, version.architecture_id)
    adapter = _adapter_for(architecture)
    personas = PersonaRepository(session)
    public_packet = personas.get_public_packet(run.persona_version_id)
    # SIMULATOR-ONLY channel: hidden truth via the guarded repository.
    hidden_truth = HiddenTruthRepository(session).get_truth(
        "candidate_simulator", run.persona_version_id)

    components = version.components_json
    limits = dict(version.limits_json or {})
    model = (version.model_config_json or {}).get("interview_model", "gpt-5.6")
    run_cap = float(limits.get("run_cost_cap_usd", 2.0))
    max_turns = int(limits.get("max_turns", 8))
    limits["max_turns"] = max_turns

    prompt_modules = _load_prompt_modules(session, version)
    built = adapter.build_session(BuildSessionRequest(
        interview_mode=run.interview_mode, public_packet=public_packet,
        components=components, prompt_modules=prompt_modules, limits=limits))

    if hooks is not None:
        pre_context = hooks.run_pre_stages(public_packet)
        if pre_context:
            built.context_block += "\n\n" + pre_context

    memory = MemoryState(strategy=components.get("memory_strategy", "full_transcript"))
    evidence_gaps: list[str] = ["ownership verification", "quantified outcomes"]
    turns: list[InterviewTurn] = []
    run.status = "in_progress"
    run.config_snapshot_json = {
        "architecture_version_id": version.id,
        "configuration_hash": version.configuration_hash,
        "components": components, "model_config": version.model_config_json,
        "limits": limits, "prompt_module_versions": version.prompt_module_versions_json,
        "interview_mode": run.interview_mode, "persona_version_id": run.persona_version_id,
        "provider_seed": run.provider_seed,
    }
    session.flush()

    def ctx(agent_role: str) -> CallContext:
        return CallContext(
            session=session, project_id=project_id, agent_role=agent_role,
            experiment_id=run.experiment_id, interview_run_id=run.id,
            experiment_cap_usd=experiment_cap_usd,
            experiment_spent_usd=experiment_spent_fn() if experiment_spent_fn else 0.0,
            run_cap_usd=run_cap, run_spent_usd=run.total_cost_usd,
            is_holdout=is_holdout, cancellation_check=cancellation_check,
            seed=run.provider_seed)

    termination_reason = None
    started = time.time()
    try:
        for turn_index in range(max_turns):
            if cancellation_check:
                cancellation_check()  # before each interview turn
            if time.time() - started > built.time_budget_seconds:
                termination_reason = "time_limit"
                break

            # ---- interviewer turn (public data + permitted context ONLY)
            turn_request = adapter.run_turn(InterviewTurnRequest(
                session=built, memory_view=interviewer_view(memory),
                transcript_tail=_transcript_text(turns[-2:]),
                turn_index=turn_index,
                evidence_gaps=evidence_gaps if components.get(
                    "follow_up_policy") == "evidence_gap_tracking" else [],
                plan=_plan_text(components, hooks, memory, evidence_gaps,
                                turn_index)))
            iv_result = call_llm(ctx("interviewer"), turn_request.messages, model,
                                 schema=InterviewerTurnSchema, handoff_to="candidate_simulator")
            iv_turn: InterviewerTurnSchema = iv_result.parsed
            if iv_turn.end_interview:
                termination_reason = "goals_satisfied"
                break

            # ---- candidate simulator turn (public + hidden packets)
            sim_messages = [
                {"role": "system", "content": SIMULATOR_SYSTEM},
                {"role": "user", "content":
                    "PUBLIC PACKET:\n" + json.dumps(public_packet, ensure_ascii=False)
                    + "\n\nHIDDEN TRUTH PACKET (never reveal directly):\n"
                    + json.dumps(hidden_truth, ensure_ascii=False)
                    + "\n\nCONVERSATION SO FAR:\n" + _transcript_text(turns)
                    + "\n\n" + wrap_untrusted(iv_turn.question, "INTERVIEWER QUESTION")
                    + "\nAnswer in persona as JSON."}]
            sim_result = call_llm(ctx("candidate_simulator"), sim_messages, model,
                                  schema=SimulatorResponseSchema, handoff_to="interviewer")
            sim: SimulatorResponseSchema = sim_result.parsed

            injection_hits = detect_injection(sim.spoken_answer)
            turn = InterviewTurn(
                interview_run_id=run.id, turn_index=turn_index,
                interviewer_text=(f"{iv_turn.reaction} {iv_turn.question}".strip()),
                candidate_text=sim.spoken_answer,
                interviewer_meta_json={
                    "internal_target": iv_turn.internal_target,
                    "is_follow_up": iv_turn.is_follow_up, "topic": iv_turn.topic,
                    "evidence_gap_targeted": iv_turn.evidence_gap_targeted,
                    "injection_signals_in_answer": injection_hits},
                simulator_protected_json={
                    "used_truth_fact_ids": sim.used_truth_fact_ids,
                    "used_exaggeration_ids": sim.used_exaggeration_ids,
                    "response_style_tags": sim.response_style_tags,
                    "simulator_consistency_warnings": sim.simulator_consistency_warnings},
                latency_ms=iv_result.latency_ms + sim_result.latency_ms,
                cost_usd=iv_result.cost_usd + sim_result.cost_usd)
            session.add(turn)
            turns.append(turn)
            run.total_latency_ms += turn.latency_ms

            # ---- memory update
            record_turn(memory, turn_index, iv_turn.question, sim.spoken_answer)
            if hooks is not None:
                for gap in hooks.after_answer(iv_turn.question, sim.spoken_answer,
                                              turn_index):
                    if gap and gap not in evidence_gaps:
                        evidence_gaps.append(gap)
            session.add(MemoryEvent(interview_run_id=run.id, turn_index=turn_index,
                                    operation="write",
                                    detail_json={"strategy": memory.strategy}))
            if memory.strategy in ("evior_candidate_memory_v2", "hybrid",
                                   "structured_summary", "event_based"):
                mem_request = adapter.update_memory(MemoryUpdateRequest(
                    memory_view=interviewer_view(memory),
                    last_question=iv_turn.question, last_answer=sim.spoken_answer))
                delta_result = call_llm(ctx("memory_manager"), mem_request.messages,
                                        model, schema=MemoryDeltaSchema)
                grounding = apply_delta(
                    memory, delta_result.parsed.model_dump(),
                    transcript_text=_transcript_text(turns),
                    source_docs=json.dumps(public_packet, ensure_ascii=False))
                session.add(MemoryEvent(
                    interview_run_id=run.id, turn_index=turn_index,
                    operation="delta_merge", detail_json=grounding))
            session.add(MemorySnapshot(
                interview_run_id=run.id, turn_index=turn_index,
                strategy=memory.strategy, memory_json=memory.to_json(),
                size_chars=context_size_chars(memory)))

            # ---- evidence ledger
            if components.get("evidence_tracking") == "structured_ledger":
                strength = ("strong" if any(ch.isdigit() for ch in sim.spoken_answer)
                            else "weak" if "vague" in sim.response_style_tags else "moderate")
                memory.evidence_ledger.append({
                    "turn_index": turn_index, "topic": iv_turn.topic,
                    "claim": sim.spoken_answer[:200], "strength": strength})
                session.add(EvidenceRecord(
                    interview_run_id=run.id, turn_index=turn_index,
                    claim=sim.spoken_answer[:500], competency=iv_turn.topic,
                    strength=strength))
                if strength == "weak" and iv_turn.topic:
                    if iv_turn.topic not in evidence_gaps:
                        evidence_gaps.append(iv_turn.topic)
                elif iv_turn.topic in evidence_gaps and strength == "strong":
                    evidence_gaps.remove(iv_turn.topic)

            run.state_json = {
                "turn_index": turn_index + 1, "question_budget": max_turns,
                "open_evidence_gaps": evidence_gaps,
                "topics_covered": list({t.interviewer_meta_json.get("topic", "")
                                        for t in turns if t.interviewer_meta_json.get("topic")}),
                "memory_size_chars": context_size_chars(memory),
                "contradictions": memory.contradictions,
                "pipeline_stage_log": (hooks.stage_log if hooks is not None else []),
            }
            session.add(InterviewStateSnapshot(
                interview_run_id=run.id, turn_index=turn_index,
                state_json=run.state_json))
            session.commit()
        else:
            termination_reason = "question_limit"
    except CancellationRequested:
        run.status = "cancelled"
        run.termination_reason = "cancellation_requested"
        session.commit()
        raise InterviewCancelled()

    # ---- final report
    if cancellation_check:
        cancellation_check()
    report_request = adapter.generate_report(ReportRequest(
        transcript=_transcript_text(turns), memory_view=interviewer_view(memory),
        interview_mode=run.interview_mode))
    report_result = call_llm(ctx("report_writer"), report_request.messages, model,
                             schema=ReportSchema)
    report: ReportSchema = report_result.parsed
    # Evidence-link validation: strip report claims without transcript evidence.
    valid_turn_ids = {f"turn-{t.turn_index}" for t in turns}
    validated_claims, dropped = [], 0
    for claim in report.claims:
        refs = [r for r in claim.get("evidence_turn_ids", []) if r in valid_turn_ids]
        if refs:
            claim["evidence_turn_ids"] = refs
            validated_claims.append(claim)
        else:
            dropped += 1
    payload = report.model_dump()
    payload["claims"] = validated_claims
    payload["unsupported_claims_dropped"] = dropped
    run.final_report_json = payload
    run.status = "completed"
    run.termination_reason = termination_reason or "completed"
    session.commit()
    return run
