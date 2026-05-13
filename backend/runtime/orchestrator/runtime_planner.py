from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict

from runtime.models.runtime_intent import RuntimeIntent


@dataclass
class PlanDescription:
    parallel_searches: int
    parallel_critics: bool
    repair_rounds_max: int
    use_agent_subgraph: bool
    use_tool_gate: bool
    parallel_drafts: bool
    hedge_draft_delay_ms: int
    layered_critics: bool


def describe_plan(intent: RuntimeIntent, cfg: Dict[str, Any]) -> PlanDescription:
    dq = cfg or {}
    n_search = int(dq.get("parallel_search_queries") or 2)
    if intent.search_score < 0.28:
        n_search = 0
    n_search = max(0, min(4, n_search))
    return PlanDescription(
        parallel_searches=n_search,
        parallel_critics=bool(dq.get("parallel_critics", True)),
        repair_rounds_max=max(1, min(4, int(dq.get("max_repair_rounds") or 2))),
        use_agent_subgraph=bool(intent.tool_requirement and dq.get("agent_subgraph_enabled")),
        use_tool_gate=bool(intent.tool_requirement and dq.get("tool_capability_gate_enabled", True)),
        parallel_drafts=bool(dq.get("parallel_drafts", True)),
        hedge_draft_delay_ms=max(0, int(dq.get("hedge_draft_delay_ms") or 0)),
        layered_critics=bool(dq.get("layered_critics", True)),
    )
