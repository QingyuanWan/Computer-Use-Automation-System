"""Write an escalation/takeover event to the evidence dir (ADR-007; schema-draft §9). Fields exactly:
escalation_at_step, reason, timestamp, human_outcome, duration_s, dom_diff_summary, operator_note."""
from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Any, Optional


def build_event(*, step_id: str, reason: str, human_outcome: str, duration_s: float,
                dom_diff_summary: Optional[Any], operator_note: Optional[str],
                timestamp: Optional[datetime.datetime] = None) -> dict[str, Any]:
    ts = timestamp or datetime.datetime.now(datetime.timezone.utc)
    return {
        "escalation_at_step": step_id,
        "reason": reason,
        "timestamp": ts.isoformat(),
        "human_outcome": human_outcome,
        "duration_s": round(float(duration_s), 3),
        "dom_diff_summary": dom_diff_summary,     # coarse structural dict, or None for non-takeover
        "operator_note": operator_note,
    }


def write_escalation_event(evidence_dir, *, step_id: str, reason: str, human_outcome: str,
                           duration_s: float, dom_diff_summary: Optional[Any],
                           operator_note: Optional[str]) -> Path:
    evidence_dir = Path(evidence_dir)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.datetime.now(datetime.timezone.utc)
    event = build_event(step_id=step_id, reason=reason, human_outcome=human_outcome,
                        duration_s=duration_s, dom_diff_summary=dom_diff_summary,
                        operator_note=operator_note, timestamp=ts)
    path = evidence_dir / f"escalation_{step_id}_{ts.strftime('%Y%m%d_%H%M%S_%f')}.json"
    path.write_text(json.dumps(event, indent=2, ensure_ascii=False), encoding="utf-8")
    return path
