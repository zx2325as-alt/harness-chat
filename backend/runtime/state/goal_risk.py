"""GoalState / RiskState：结构化目标与风险视图（写入 ExecutionState 快照字典）。"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List


@dataclass
class GoalState:
    goals: List[str] = field(default_factory=list)
    resolved: List[str] = field(default_factory=list)
    unresolved: List[str] = field(default_factory=list)
    progress_hint: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class RiskState:
    risk_score: float = 0.0
    ambiguity: float = 0.0
    high_risk_domain: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


__all__ = ["GoalState", "RiskState"]
