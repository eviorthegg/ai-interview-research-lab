"""Memory strategies available to architectures.

Each strategy defines: what is stored, when updated, what the interviewer sees,
maximum size, and grounding requirements. The evior_candidate_memory_v2
strategy re-implements the production principle: the LLM proposes a DELTA,
code performs the merge, and new factual claims must be grounded in the
transcript (fact grounding).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

COVERAGE_DIMENSIONS = [
    "technical_depth", "technical_breadth", "ownership_delivery",
    "collaboration_conflict", "adaptability_learning", "leadership_influence",
    "motivation_goals", "work_values", "logistics_facts", "communication",
]

STRATEGY_SPECS = {
    "stateless": {
        "stores": "nothing", "updated": "never", "interviewer_sees": "current turn only",
        "max_chars": 0, "grounding": "n/a",
    },
    "full_transcript": {
        "stores": "every turn verbatim", "updated": "every turn",
        "interviewer_sees": "entire transcript", "max_chars": 60000, "grounding": "verbatim",
    },
    "rolling_window": {
        "stores": "last N turns", "updated": "every turn",
        "interviewer_sees": "last 6 turns", "max_chars": 12000, "grounding": "verbatim",
    },
    "structured_summary": {
        "stores": "compressed summary", "updated": "every 3 turns",
        "interviewer_sees": "summary + last turn", "max_chars": 4000,
        "grounding": "summary must cite turn indexes",
    },
    "event_based": {
        "stores": "typed events (claim/evidence/contradiction)", "updated": "every turn",
        "interviewer_sees": "event list", "max_chars": 8000, "grounding": "per-event turn ref",
    },
    "evior_candidate_memory_v2": {
        "stores": "facts, skill matrix (probed_depth 0-3), coverage map, asked questions,"
                  " risks, contradictions", "updated": "delta-merge every turn",
        "interviewer_sees": "WHAT YOU KNOW / SUSPECT / DON'T KNOW / GOALS / ALREADY ASKED",
        "max_chars": 6000, "grounding": "facts must appear in transcript or CV/JD",
    },
    "hybrid": {
        "stores": "recent turns + structured memory + evidence ledger",
        "updated": "every turn", "interviewer_sees": "recent 4 turns + briefing + ledger",
        "max_chars": 10000, "grounding": "facts grounded; ledger items carry turn refs",
    },
}


@dataclass
class MemoryState:
    strategy: str
    turns: list[dict] = field(default_factory=list)  # {turn_index, question, answer}
    summary: str = ""
    events: list[dict] = field(default_factory=list)
    facts: dict = field(default_factory=dict)
    skills: list[dict] = field(default_factory=list)          # {name, probed_depth}
    coverage: dict = field(default_factory=lambda: {d: 0.0 for d in COVERAGE_DIMENSIONS})
    asked_questions: list[str] = field(default_factory=list)
    risks: list[dict] = field(default_factory=list)
    contradictions: list[dict] = field(default_factory=list)
    evidence_ledger: list[dict] = field(default_factory=list)
    memory_ops: int = 0

    def to_json(self) -> dict:
        return {
            "strategy": self.strategy, "turns": self.turns, "summary": self.summary,
            "events": self.events, "facts": self.facts, "skills": self.skills,
            "coverage": self.coverage, "asked_questions": self.asked_questions,
            "risks": self.risks, "contradictions": self.contradictions,
            "evidence_ledger": self.evidence_ledger, "memory_ops": self.memory_ops}

    @classmethod
    def from_json(cls, data: dict) -> "MemoryState":
        state = cls(strategy=data.get("strategy", "full_transcript"))
        for key in ("turns", "summary", "events", "facts", "skills", "coverage",
                    "asked_questions", "risks", "contradictions", "evidence_ledger",
                    "memory_ops"):
            if key in data:
                setattr(state, key, data[key])
        return state


def record_turn(state: MemoryState, turn_index: int, question: str, answer: str) -> None:
    """Code-side update after each turn (delta-merge principle: code owns the merge)."""
    state.memory_ops += 1
    if state.strategy == "stateless":
        return
    state.turns.append({"turn_index": turn_index, "question": question, "answer": answer})
    state.asked_questions.append(question)
    if state.strategy == "rolling_window":
        state.turns = state.turns[-6:]
    if state.strategy in ("event_based", "hybrid"):
        state.events.append({"type": "answer", "turn_index": turn_index,
                             "text": answer[:400]})


def apply_delta(state: MemoryState, delta: dict, transcript_text: str,
                source_docs: str = "") -> dict:
    """Merge an LLM-proposed delta. FACT GROUNDING: a new fact is accepted only
    when its value appears in the transcript or provided CV/JD text.
    Returns {"accepted": [...], "rejected_ungrounded": [...]}."""
    state.memory_ops += 1
    accepted, rejected = [], []
    haystack = (transcript_text + " " + source_docs).lower()
    for key, value in (delta.get("facts") or {}).items():
        needle = str(value).lower().strip()
        if needle and (needle in haystack or len(needle) < 3):
            state.facts[key] = value
            accepted.append(f"fact:{key}")
        else:
            rejected.append(f"fact:{key}")
    existing_skills = {s["name"].lower(): s for s in state.skills}
    for skill in delta.get("skills") or []:
        name = str(skill.get("name", "")).strip()
        if not name:
            continue
        depth = max(0, min(3, int(skill.get("probed_depth", 0))))
        current = existing_skills.get(name.lower())
        if current:
            current["probed_depth"] = max(current["probed_depth"], depth)  # never silently downgrade
        else:
            state.skills.append({"name": name, "probed_depth": depth})
        accepted.append(f"skill:{name}")
    for risk in delta.get("risks") or []:
        state.risks.append(risk)
    for contradiction in delta.get("contradictions") or []:
        state.contradictions.append(contradiction)  # flagged, never deleted
    for dim, score in (delta.get("coverage_updates") or {}).items():
        if dim in state.coverage:
            state.coverage[dim] = max(state.coverage[dim], min(1.0, float(score)))
    # Explicit removals require a reason (no memory erosion)
    for removal in delta.get("remove") or []:
        if removal.get("reason"):
            target = str(removal.get("target", ""))
            state.skills = [s for s in state.skills if s["name"] != target]
    return {"accepted": accepted, "rejected_ungrounded": rejected}


def interviewer_view(state: MemoryState) -> str:
    """What the interviewer sees, per strategy. Size-capped."""
    spec = STRATEGY_SPECS[state.strategy]
    max_chars = spec["max_chars"]
    if state.strategy == "stateless":
        return "(no memory available — stateless strategy)"
    if state.strategy == "full_transcript":
        text = "\n".join(f"Q{t['turn_index']}: {t['question']}\nA{t['turn_index']}: {t['answer']}"
                         for t in state.turns)
        return text[-max_chars:] if text else "(no turns yet)"
    if state.strategy == "rolling_window":
        text = "\n".join(f"Q{t['turn_index']}: {t['question']}\nA{t['turn_index']}: {t['answer']}"
                         for t in state.turns[-6:])
        return text[-max_chars:] if text else "(no turns yet)"
    if state.strategy == "structured_summary":
        last = state.turns[-1] if state.turns else None
        parts = [f"SUMMARY SO FAR:\n{state.summary or '(none yet)'}"]
        if last:
            parts.append(f"LAST TURN:\nQ: {last['question']}\nA: {last['answer']}")
        return "\n\n".join(parts)[:max_chars]
    if state.strategy == "event_based":
        return json.dumps(state.events[-30:], ensure_ascii=False)[:max_chars]
    if state.strategy == "evior_candidate_memory_v2":
        return _memory_v2_briefing(state)[:max_chars]
    if state.strategy == "hybrid":
        recent = "\n".join(f"Q{t['turn_index']}: {t['question']}\nA{t['turn_index']}: {t['answer']}"
                           for t in state.turns[-4:])
        ledger = json.dumps(state.evidence_ledger[-10:], ensure_ascii=False)
        return (f"{_memory_v2_briefing(state)}\n\nRECENT TURNS:\n{recent}\n\n"
                f"EVIDENCE LEDGER (latest):\n{ledger}")[:max_chars]
    return "(unknown strategy)"


def _memory_v2_briefing(state: MemoryState) -> str:
    """Production-style briefing: WHAT YOU KNOW / SUSPECT / DON'T KNOW /
    SESSION GOALS / ALREADY ASKED (mirrors candidate_context.py)."""
    depth_labels = {0: "claimed only — NOT yet verified", 1: "explained real usage",
                    2: "explained trade-offs/failures", 3: "survived adversarial follow-up"}
    known = [f"- {k}: {v}" for k, v in state.facts.items()]
    known += [f"- skill {s['name']}: {depth_labels[s['probed_depth']]}"
              for s in state.skills if s["probed_depth"] >= 1]
    suspect = [f"- skill {s['name']}: {depth_labels[0]}"
               for s in state.skills if s["probed_depth"] == 0]
    suspect += [f"- risk: {r.get('description', r)}" for r in state.risks[-5:]]
    suspect += [f"- contradiction: {c.get('description', c)}"
                for c in state.contradictions[-3:]]
    gaps = [f"- {dim} (coverage {score:.1f})" for dim, score in
            sorted(state.coverage.items(), key=lambda kv: kv[1])[:5]]
    goals = [f"- Probe {dim} — lowest coverage" for dim, score in
             sorted(state.coverage.items(), key=lambda kv: kv[1])[:3]]
    asked = [f"- {q}" for q in state.asked_questions[-15:]]
    return ("WHAT YOU KNOW\n" + ("\n".join(known) or "(nothing confirmed yet)")
            + "\n\nWHAT YOU SUSPECT\n" + ("\n".join(suspect) or "(nothing)")
            + "\n\nWHAT YOU DON'T KNOW\n" + ("\n".join(gaps) or "(fully covered)")
            + "\n\nTHIS SESSION'S GOALS\n" + ("\n".join(goals) or "(none)")
            + "\n\nALREADY ASKED — never repeat these\n" + ("\n".join(asked) or "(none)"))


def context_size_chars(state: MemoryState) -> int:
    return len(interviewer_view(state))
