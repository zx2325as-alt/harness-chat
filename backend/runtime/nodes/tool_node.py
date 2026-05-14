"""Tool Capability DAG Node：在图中显式启用 tool_use 能力（§一 Tool Nodes）。"""
from __future__ import annotations

from refine_shared import _pg
from runtime.kernel.runtime_context import DAGRuntimeContext
from runtime_state import GoalExecutionState


async def execute_tool_gate(ctx: DAGRuntimeContext) -> None:
    ctx.runtime.enable("tool_use", reason="runtime_planner_tool_gate")
    gate = {"enabled": True, "mode": "dag_capability"}
    ctx.options["_tool_runtime_gate"] = gate
    goal_exec = ctx.options.get("_goal_execution_state")
    if isinstance(goal_exec, GoalExecutionState):
        goal_exec.tool_results["tool_gate"] = gate
    await ctx.emit(
        {
            "event": "step",
            "step": {
                "name": "dag_tool_capability_gate",
                "status": "ok",
                "meta": _pg({"capability": "tool_use"}, "reasoning", "Runtime：tool_use 能力层已启用（DAG 节点）。"),
            },
        }
    )
