"""Runtime Planner 视图：由 Intent + PlanDescription 生成可序列化的动态 DAG 规格摘要。"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict

from runtime.models.runtime_intent import RuntimeIntent

if TYPE_CHECKING:
    from runtime.orchestrator.runtime_planner import PlanDescription


def describe_dynamic_plan(intent: RuntimeIntent, plan: "PlanDescription", prompt: str) -> Dict[str, Any]:
    del prompt
    needs_search = plan.parallel_searches > 0
    parallel = []
    if needs_search:
        parallel.append("parallel_web_search")
    parallel.append("parallel_draft_generation")
    then = []
    if plan.layered_critics:
        then.append("layered_parallel_critics")
    elif plan.parallel_critics:
        then.append("paired_parallel_critics")
    else:
        then.append("unified_critic")
    then.extend(["targeted_repair_loop", "verify_answer", "deterministic_finalize"])
    caps = ["search", "draft", "critic", "repair", "verify", "finalize"]
    if plan.use_tool_gate:
        caps.insert(2, "tool_use_gate")
    if plan.use_goal_subgraph:
        caps.insert(3, "goal_capability_gate")
    return {
        "parallel_wave_1": parallel,
        "sequential_core": then,
        "if_needs_search_followup": ["additional_parallel_search"],
        "if_verify_search_more": ["extend_repair_round"],
        "budget": {
            "latency_tier": intent.latency_budget,
            "quality_tier": intent.quality_requirement,
            "max_repair_rounds": plan.repair_rounds_max,
        },
        "capabilities": caps,
    }
