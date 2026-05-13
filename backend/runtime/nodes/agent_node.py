"""Goal-Oriented Agent 子图占位（plan.use_agent_subgraph 时可扩展）。"""
from __future__ import annotations

from runtime.kernel.runtime_context import DAGRuntimeContext
from runtime_state import AgentState


async def execute_optional(ctx: DAGRuntimeContext) -> None:
    if not getattr(ctx.plan, "use_agent_subgraph", False):
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
        "model": "goal_oriented_stub",
    }
    ctx.options["_agent_runtime_gate"] = gate
    ag = ctx.options.get("_agent_state")
    if isinstance(ag, AgentState):
        ag.blocked_reasons = [] if not blocked else ["pending_goals_or_evidence"]
        ag.tool_results["runtime_gate"] = gate
        ag.progress_score = 1.0 if (goals_done and evidence_ok) else max(0.0, 1.0 - 0.12 * max(1, len(unresolved)))
    await ctx.emit(
        {
            "event": "step",
            "step": {
                "name": "agent_subgraph_goal_gate",
                "status": "skipped" if blocked else "ok",
                "meta": {
                    "reason": "agent_subgraph_stub",
                    "goals_resolved": goals_done,
                    "unresolved_count": len(unresolved),
                    "evidence_count": n_ev,
                    "evidence_sufficient": evidence_ok,
                    "would_run_tools": not blocked,
                },
            },
        }
    )
