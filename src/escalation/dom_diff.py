"""Coarse, privacy-preserving DOM diff (ADR-007). A snapshot is a STRUCTURAL histogram — element counts
keyed by ARIA role (or tag) — with NO text content, so the summary written to evidence can never carry
human-entered credentials/PII. The diff reports added/removed/mutated counts + a per-role delta."""
from __future__ import annotations

from typing import Any

# Evaluated in-page to produce a snapshot. Marker in the comment lets tests recognize the snapshot call.
DOM_SNAPSHOT_JS = r"""/* IFAI_DOM_SNAPSHOT */
() => {
  const counts = {};
  for (const el of document.querySelectorAll('*')) {
    const role = el.getAttribute('role') || el.tagName.toLowerCase();
    counts[role] = (counts[role] || 0) + 1;
  }
  return counts;                    // structural histogram ONLY — never element text (ADR-7 privacy)
}"""


def summarize(before: dict[str, int], after: dict[str, int]) -> dict[str, Any]:
    """Coarse structural delta between two role-count snapshots. `mutated` is 0 in Phase A — count-based
    snapshots cannot detect in-place attribute changes; attribute-level mutation tracking is deferred."""
    roles = set(before or {}) | set(after or {})
    added = 0
    removed = 0
    by_role: dict[str, int] = {}
    for role in sorted(roles):
        delta = int(after.get(role, 0)) - int(before.get(role, 0))
        if delta > 0:
            added += delta
            by_role[role] = delta
        elif delta < 0:
            removed += -delta
            by_role[role] = delta
    return {"added": added, "removed": removed, "mutated": 0, "by_role": by_role}
