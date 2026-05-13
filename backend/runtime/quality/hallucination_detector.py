from __future__ import annotations

from typing import Any, Dict


def hallucination_score_from_critics(unified: Dict[str, Any], structured: Dict[str, Any]) -> float:
    try:
        u = float(unified.get("hallucination_risk") or 0.2)
    except (TypeError, ValueError):
        u = 0.2
    fr = structured.get("fact_risks") or []
    bump = min(0.35, 0.04 * min(len(fr), 8))
    return max(0.0, min(1.0, u + bump))
