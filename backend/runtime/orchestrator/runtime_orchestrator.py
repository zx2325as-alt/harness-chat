from __future__ import annotations

from typing import Any, Callable, Dict

from runtime.models.runtime_intent import RuntimeIntent, intent_from_analysis
from runtime.orchestrator.runtime_planner import PlanDescription, describe_plan


class RuntimeOrchestrator:
    """Intent → Plan 元数据；主执行流在 dag_stream（保留 hooks 供扩展）。"""

    def __init__(self, hcfg: Dict[str, Any]) -> None:
        self.hcfg = hcfg
        self.dag_cfg = hcfg.get("dag_runtime") if isinstance(hcfg.get("dag_runtime"), dict) else {}

    def intent_from_analysis(self, analysis: Dict[str, Any]) -> RuntimeIntent:
        return intent_from_analysis(analysis)

    def plan(self, intent: RuntimeIntent) -> PlanDescription:
        return describe_plan(intent, self.dag_cfg)
