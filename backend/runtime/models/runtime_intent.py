from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict


@dataclass
class RuntimeIntent:
    """Analyzer 仅输出连续意图分数与预算档位，不再绑定 fast/refine/agent 互斥轨。"""

    reasoning_score: float
    search_score: float
    risk_score: float
    latency_budget: str
    quality_requirement: str
    realtime_requirement: bool
    tool_requirement: bool
    ambiguity_score: float

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _clamp01(x: Any, default: float = 0.0) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        v = default
    return max(0.0, min(1.0, v))


def intent_from_legacy_analysis(analysis: Dict[str, Any]) -> RuntimeIntent:
    """将现有 analyze_complexity 字典映射为 RuntimeIntent（迁移期兼容）。"""
    cx = str(analysis.get("complexity") or "low").lower()
    reasoning = 0.85 if cx == "high" else 0.62 if cx == "medium" else 0.38

    si = str(analysis.get("search_intent") or "").lower()
    sr = bool(analysis.get("search_required"))
    search_sc = 0.9 if sr or si in ("explicit", "required", "freshness_required") else 0.35
    if si == "none":
        search_sc = min(search_sc, 0.2)

    risk = 0.75 if analysis.get("high_risk_domain") else 0.38
    conf = _clamp01(analysis.get("confidence"), 0.55)
    ambiguity = max(0.05, 1.0 - conf)

    task = str(analysis.get("task_type") or "generation").lower()
    quality = "high" if cx == "high" or task in ("reasoning", "code") else "medium"

    latency = "medium"
    if cx == "low" and task == "conversation":
        latency = "low"
    if sr or si in ("explicit", "required"):
        latency = "high"

    realtime = sr or si in ("explicit", "required", "freshness_required")
    tool = task in ("reasoning", "code") or bool(analysis.get("high_risk_domain"))

    return RuntimeIntent(
        reasoning_score=reasoning,
        search_score=search_sc,
        risk_score=risk,
        latency_budget=latency,
        quality_requirement=quality,
        realtime_requirement=realtime,
        tool_requirement=tool,
        ambiguity_score=ambiguity,
    )
