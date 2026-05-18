"""Goal Capability Gate：LLM 分解 subgoals → 工具执行 → 进度追踪 → goal_completed 停止。"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from refine_shared import _pg
from runtime.kernel.runtime_context import DAGRuntimeContext
from runtime.state.evidence_state import EvidenceGraph, EvidenceNode
from runtime_state import GoalExecutionState


# ---------------------------------------------------------------------------
# LLM goal decomposition
# ---------------------------------------------------------------------------

_DECOMPOSE_PROMPT = """\
你是一个目标规划器。请分析用户的请求，识别其中的子目标，并判断哪些目标仍需要收集证据。

用户问题：
{prompt}

已有目标列表：
{goals}

当前已收集证据摘要（前1000字）：
{evidence_summary}

请返回严格的 JSON，不要有任何额外文本：
{{
  "subgoals": ["子目标1", "子目标2"],
  "search_queries": ["查询1", "查询2"],
  "goals_resolved": ["已解决的目标"],
  "goals_unresolved": ["未解决的目标"],
  "completion_confident": true/false,
  "reasoning": "简短理由"
}}
"""


def _parse_goal_plan(text: str) -> Dict[str, Any]:
    """从 LLM 输出中提取 JSON goal plan，带 fallback。"""
    text = str(text or "").strip()
    # 尝试提取 ```json ... ``` 块
    m = re.search(r"```(?:json)?\s*([\s\S]+?)```", text)
    if m:
        text = m.group(1).strip()
    # 尝试提取第一个 { ... } 块
    m2 = re.search(r"\{[\s\S]+\}", text)
    if m2:
        text = m2.group(0)
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, ValueError):
        pass
    return {}


async def _llm_decompose_goals(
    ctx: DAGRuntimeContext,
    goals: List[str],
    evidence_summary: str,
) -> Dict[str, Any]:
    """调用 LLM 分解目标，返回结构化 goal plan。失败时返回空 dict。"""
    h = ctx.harness
    opt = ctx.options
    hcfg = ctx.hcfg
    models = (ctx.quality_models.get("draft") or [ctx.default_model]) if ctx.quality_models else []
    if not models:
        models = [ctx.default_model] if ctx.default_model else []

    goals_text = "\n".join(f"- {g}" for g in goals[:8]) if goals else "（无明确子目标）"
    prompt = _DECOMPOSE_PROMPT.format(
        prompt=ctx.prompt[:600],
        goals=goals_text,
        evidence_summary=evidence_summary[:1000],
    )
    try:
        opts_g = {**opt, "_skip_search": True, "_skip_quality": True}
        r, _ = await h._ask_with_fallback(models, prompt, opts_g, messages=None)
        if r and r.success:
            plan = _parse_goal_plan(r.content or "")
            # wire real token counts into budget
            if ctx.budget and (r.tokens_in or r.tokens_out):
                ctx.budget.note_llm_cost(tokens_in=r.tokens_in, tokens_out=r.tokens_out)
            return plan
    except Exception:
        pass
    return {}


# ---------------------------------------------------------------------------
# Goal completion scoring
# ---------------------------------------------------------------------------

def _goal_completion_score(
    goals: List[str],
    plan: Dict[str, Any],
    n_evidence: int,
    entry_search_required: bool,
) -> float:
    """0.0–1.0 进度评分：以证据覆盖 + LLM plan 综合计算。"""
    resolved = list(plan.get("goals_resolved") or [])
    unresolved = list(plan.get("goals_unresolved") or [])
    if not goals:
        return 1.0 if n_evidence >= 1 else 0.8
    total = max(len(goals), 1)
    resolved_ratio = len(resolved) / total if resolved else 0.0
    evidence_ok = n_evidence >= 2 or not entry_search_required
    confident = bool(plan.get("completion_confident", False))
    base = resolved_ratio * 0.6 + (0.2 if evidence_ok else 0.0) + (0.2 if confident else 0.0)
    return min(1.0, base)


# ---------------------------------------------------------------------------
# Tool execution for gaps
# ---------------------------------------------------------------------------

async def _execute_tool_queries(
    ctx: DAGRuntimeContext,
    queries: List[str],
    goal_exec: Optional[GoalExecutionState],
) -> List[Any]:
    """对 LLM 指定的 search_queries 执行 web_search，结果写入 EvidenceGraph。"""
    from runtime.nodes.tool_node import execute_tool, tool_result_to_evidence_node

    results = []
    for q in queries[:3]:
        q = str(q).strip()
        if not q or (goal_exec and q in goal_exec.search_history):
            continue
        tr = await execute_tool(ctx, "web_search", q)
        results.append(tr)
        if goal_exec:
            goal_exec.search_history.append(q[:200])
        if tr.ok:
            node = tool_result_to_evidence_node(tr)
            if ctx.evidence_graph is None:
                ctx.evidence_graph = EvidenceGraph(nodes=[node])
            else:
                ctx.evidence_graph.nodes.append(node)
            ctx.evidence_objs.append(node)
    return results


# ---------------------------------------------------------------------------
# DAG 节点入口
# ---------------------------------------------------------------------------

async def execute_optional(ctx: DAGRuntimeContext) -> None:
    """Goal Capability Gate：LLM 分解目标 → 工具填补证据缺口 → 进度追踪。"""
    if not getattr(ctx.plan, "use_goal_subgraph", False):
        return

    st = ctx.st
    unresolved: List[str] = list(getattr(st, "unresolved_goals", None) or []) if st else []
    goals: List[str] = list(getattr(st, "goals", None) or []) if st else []
    n_ev = len(getattr(ctx, "evidence_objs", None) or [])
    evidence_summary = (ctx.ev_text or "")[:1000]
    goal_exec: Optional[GoalExecutionState] = ctx.options.get("_goal_execution_state")
    if not isinstance(goal_exec, GoalExecutionState):
        goal_exec = GoalExecutionState()
        ctx.options["_goal_execution_state"] = goal_exec

    await ctx.emit({"event": "step", "step": {
        "name": "goal_capability_gate",
        "status": "running",
        "meta": _pg(
            {"goals": goals[:8], "unresolved": unresolved[:8], "evidence_count": n_ev},
            "reasoning",
            "Goal Capability Gate：LLM 分解目标 + 工具填补证据缺口",
        ),
    }})

    # Step 1: LLM goal decomposition
    plan = await _llm_decompose_goals(ctx, unresolved or goals, evidence_summary)

    # Step 2: tool execution for identified search gaps
    search_queries = [str(q).strip() for q in (plan.get("search_queries") or []) if str(q).strip()]
    tool_results = []
    if search_queries and not ctx.blocked:
        tool_results = await _execute_tool_queries(ctx, search_queries, goal_exec)
        n_ev = len(getattr(ctx, "evidence_objs", None) or [])

    # Step 3: update goal state based on plan
    subgoals = [str(s).strip() for s in (plan.get("subgoals") or []) if str(s).strip()]
    resolved_by_llm = [str(g).strip() for g in (plan.get("goals_resolved") or []) if str(g).strip()]
    still_unresolved = [str(g).strip() for g in (plan.get("goals_unresolved") or []) if str(g).strip()]

    if st and resolved_by_llm:
        for g in resolved_by_llm:
            if g in st.unresolved_goals:
                st.unresolved_goals.remove(g)
            if g not in st.resolved_goals:
                st.resolved_goals.append(g)
    if st and subgoals:
        st.goals = list(dict.fromkeys(st.goals + subgoals))

    progress = _goal_completion_score(
        goals or unresolved,
        plan,
        n_ev,
        ctx.entry_search_required,
    )
    goals_done = progress >= 0.8 or bool(plan.get("completion_confident"))
    evidence_ok = n_ev >= 2 or not ctx.entry_search_required
    blocked = not (goals_done and evidence_ok)

    goal_exec.subgoals = subgoals or goal_exec.subgoals
    goal_exec.unresolved_goals = still_unresolved or (unresolved if not goals_done else [])
    goal_exec.solved_goals = resolved_by_llm or (goals if goals_done else [])
    goal_exec.progress_score = progress
    goal_exec.blocked_reasons = [] if not blocked else (
        (["insufficient_evidence"] if not evidence_ok else []) +
        (["unresolved_goals"] if not goals_done else [])
    )
    goal_exec.tool_results["goal_gate"] = {
        "tools_executed": len(tool_results),
        "tools_ok": sum(1 for r in tool_results if r.ok),
        "search_queries": search_queries,
        "plan_confident": bool(plan.get("completion_confident")),
        "progress_score": progress,
    }

    if st:
        st.runtime_memory.append({
            "phase": "goal_capability_gate",
            "goals_total": len(goals),
            "goals_resolved": len(resolved_by_llm),
            "tools_executed": len(tool_results),
            "progress": progress,
        })

    await ctx.emit({"event": "step", "step": {
        "name": "goal_capability_gate",
        "status": "skipped" if blocked else "ok",
        "meta": _pg(
            {
                "goals_resolved": len(resolved_by_llm),
                "unresolved_count": len(still_unresolved),
                "subgoals": subgoals[:6],
                "tools_executed": len(tool_results),
                "evidence_count": n_ev,
                "progress_score": round(progress, 3),
                "completion_confident": bool(plan.get("completion_confident")),
                "blocked": blocked,
                "llm_reasoning": str(plan.get("reasoning") or "")[:300],
            },
            "reasoning",
            f"Goal Gate 完成（进度 {progress:.0%}，工具调用 {len(tool_results)} 次）",
        ),
    }})
