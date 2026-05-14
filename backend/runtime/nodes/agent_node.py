"""Goal capability gate 占位（plan.use_goal_subgraph 时可扩展）。"""
from __future__ import annotations

from runtime.kernel.runtime_context import DAGRuntimeContext
from runtime_state import GoalExecutionState


async def execute_optional(ctx: DAGRuntimeContext) -> None:
    if not getattr(ctx.plan, "use_goal_subgraph", False):
        return
    st = getattr(ctx, "st", None)
    unresolved = list(getattr(st, "unresolved_goals", None) or []) if st else []
    goals_done = len(unresolved) == 0
    n_ev = len(getattr(ctx, "evidence_objs", None) or [])
    evidence_ok = n_ev >= 2 or not getattr(ctx, "entry_search_required", False)
    blocked = not (goals_done and evidence_ok)
    gate = {
        "goals_resolved": goals_done,
        "evidence_sufficient": evidence_ok,
        "blocked": blocked,
        "model": "goal_capability_stub",
    }
    ctx.options["_goal_capability_gate"] = gate
    goal_exec = ctx.options.get("_goal_execution_state")
    if isinstance(goal_exec, GoalExecutionState):
        goal_exec.blocked_reasons = [] if not blocked else ["pending_goals_or_evidence"]
        goal_exec.tool_results["goal_gate"] = gate
        goal_exec.progress_score = 1.0 if (goals_done and evidence_ok) else max(0.0, 1.0 - 0.12 * max(1, len(unresolved)))
    await ctx.emit(
        {
            "event": "step",
            "step": {
                "name": "goal_capability_gate",
                "status": "skipped" if blocked else "ok",
                "meta": {
                    "reason": "goal_capability_stub",
                    "goals_resolved": goals_done,
                    "unresolved_count": len(unresolved),
                    "evidence_count": n_ev,
                    "evidence_sufficient": evidence_ok,
                    "would_run_tools": not blocked,
                },
            },
        }
    )
