"""
Adaptive Self-Correcting Async DAG AI Runtime：入口编排 + DAGScheduler 波次 gather。
Intent → Planner → Execution DAG（search → draft → quality → finalize）→ Metrics。
"""
from __future__ import annotations

import re
import time
from typing import Any, AsyncGenerator, Callable, Dict, List, Optional

from refine_shared import _pg
from runtime.cache.runtime_cache import RuntimeTieredCaches
from runtime.dag_common import project_analysis_for_dag_runtime, sync_dag_execution_layer
from runtime.kernel.runtime_context import DAGRuntimeContext
from runtime.kernel.runtime_executor import build_execution_dag, planned_dag_node_ids, stream_scheduled_dag
from runtime.orchestrator.budget_manager import BudgetManager
from runtime.orchestrator.runtime_orchestrator import RuntimeOrchestrator
from runtime.models.planner_model import describe_dynamic_plan
from runtime.streaming.progressive_stream import ProgressiveStreamRouter
from runtime.state.goal_risk import GoalState, RiskState
from runtime.state.semantic_memory import SemanticMemory
from runtime_state import GoalExecutionState, get_execution_state

from harness import SSE_PROTOCOL_META, _analyze_step_summary


def _apply_memory_hints(plan: Any, options: Dict[str, Any]) -> None:
    """读取 SemanticMemory 历史，根据成功/失败模式微调当前 plan。"""
    sm = options.get("_dag_semantic_memory")
    if not isinstance(sm, SemanticMemory) or not sm.entries:
        return
    turns = [e for e in sm.entries if e.get("kind") == "turn"]
    if not turns:
        return
    recent = turns[-8:]  # 最近 8 轮
    fail_count = sum(1 for t in recent if not t.get("ok", True))
    ok_count = sum(1 for t in recent if t.get("ok", True))
    total = max(len(recent), 1)
    fail_rate = fail_count / total

    # 高失败率 → 增加 repair 轮次（上限 3）
    if fail_rate >= 0.5 and plan.repair_rounds_max < 3:
        plan.repair_rounds_max = min(3, plan.repair_rounds_max + 1)

    # 历史多轮 ok 且均无 search → 降低 parallel_searches
    search_scores = [float((t.get("intent") or {}).get("search_score") or 0) for t in recent if t.get("ok")]
    avg_search = sum(search_scores) / max(len(search_scores), 1) if search_scores else 0.0
    if ok_count >= 3 and avg_search < 0.25 and plan.parallel_searches > 1:
        plan.parallel_searches = max(1, plan.parallel_searches - 1)

    # 历史失败轮次 search_score 高 → 提升检索
    fail_search = [float((t.get("intent") or {}).get("search_score") or 0) for t in recent if not t.get("ok")]
    avg_fail_search = sum(fail_search) / max(len(fail_search), 1) if fail_search else 0.0
    if fail_rate >= 0.4 and avg_fail_search > 0.5 and plan.parallel_searches < 4:
        plan.parallel_searches = min(4, plan.parallel_searches + 1)


def _analysis_payload_for_dag_sse(analysis: Dict[str, Any]) -> Dict[str, Any]:
    """DAG 主路径：SSE 元数据仅暴露运行时研判与 runtime intent。"""
    return dict(analysis) if isinstance(analysis, dict) else {}


async def run_dag_runtime_stream(
    harness: Any,
    prompt: str,
    mode: str,
    options: Dict[str, Any],
    messages: Optional[List[Dict[str, Any]]],
    trace_id: str,
    hcfg: Dict[str, Any],
    prep: Dict[str, Any],
    _tag: Callable[..., Dict[str, Any]],
) -> AsyncGenerator[Dict[str, Any], None]:
    analysis = prep["analysis"]
    entry_search_required = prep["entry_search_required"]
    search_reason = prep["search_reason"]

    orch = RuntimeOrchestrator(hcfg)
    intent = orch.intent_from_analysis(analysis)
    plan = orch.plan(intent)
    # SemanticMemory：用历史成功/失败模式微调 plan
    _apply_memory_hints(plan, options)
    # max_quality_mode：强制最大质量参数，覆盖 planner 决策
    _dgc_cfg = hcfg.get("dag_runtime") if isinstance(hcfg.get("dag_runtime"), dict) else {}
    if bool(_dgc_cfg.get("max_quality_mode")):
        plan.repair_rounds_max = max(plan.repair_rounds_max, 3)
        plan.layered_critics = True
        plan.parallel_critics = True
        plan.parallel_drafts = True
        plan.hedge_draft_delay_ms = 0   # synthesis 替代 hedge
        options["_dag_cost_efficient"] = False
        # 放宽 latency_budget 至 high，让 BudgetManager 阈值最宽
        try:
            intent.latency_budget = "high"
            intent.quality_requirement = "high"
        except Exception:
            pass
    # Tier2.1: 简单对话快速旁路——高置信 conversation 且无检索/文档需求时，
    # 把 DAG 收敛为 draft→finalize 的单次强模型直答，跳过 critic/repair/polish。
    options["_dag_fast_path"] = False
    if not bool(_dgc_cfg.get("max_quality_mode")):
        try:
            _conf = float(analysis.get("confidence") or 0.0)
        except (TypeError, ValueError):
            _conf = 0.0
        _si = str(analysis.get("search_intent") or "none").lower()
        _docs = options.get("documents")
        _has_docs = isinstance(_docs, list) and len(_docs) > 0
        _fast_eligible = (
            str(analysis.get("task_type") or "").lower() == "conversation"
            and _conf >= 0.75
            and not bool(prep.get("entry_search_required"))
            and not bool(prep.get("search_mandatory"))
            and _si in ("none", "")
            and not bool(analysis.get("search_required"))
            and not _has_docs
        )
        if _fast_eligible:
            options["_dag_fast_path"] = True
            plan.parallel_searches = 0
            plan.repair_rounds_max = 0
            plan.parallel_drafts = False
            plan.hedge_draft_delay_ms = 0
            plan.layered_critics = False
            plan.parallel_critics = False
            plan.use_goal_subgraph = False
            plan.use_tool_gate = False

    intent_dict = intent.to_dict()
    options["_analysis_full"] = dict(analysis)
    analysis_projected = project_analysis_for_dag_runtime(analysis)
    analysis_sse = {
        **_analysis_payload_for_dag_sse(analysis),
        "runtime_intent": intent_dict,
        "runtime_contract": prep.get("runtime_contract"),
        "dag_analysis_projection": True,
    }

    sync_dag_execution_layer(options, intent)

    step_analyze = {
        "name": "complexity_analyze",
        "status": "ok",
        "meta": _pg(
            {**analysis_sse, **_tag("intake")},
            "intake",
            _analyze_step_summary(analysis_sse),
        ),
        "input_preview": (prompt[:240] + ("…" if len(prompt) > 240 else "")),
    }
    yield {"event": "step", "step": step_analyze}

    dyn_plan = describe_dynamic_plan(intent, plan, prompt)
    yield {
        "event": "step",
        "step": {
            "name": "dag_runtime_plan",
            "status": "ok",
            "meta": _pg(
                {
                    **_tag("routing"),
                    "architecture": "adaptive_dag_async_v3",
                    "runtime_intent": intent_dict,
                    "plan": {
                        "parallel_search_queries": plan.parallel_searches,
                        "parallel_critics": plan.parallel_critics,
                        "layered_critics": plan.layered_critics,
                        "parallel_drafts": plan.parallel_drafts,
                        "hedge_draft_delay_ms": plan.hedge_draft_delay_ms,
                        "max_repair_rounds": plan.repair_rounds_max,
                        "goal_subgraph": plan.use_goal_subgraph,
                        "tool_capability_gate": plan.use_tool_gate,
                        "planned_nodes": planned_dag_node_ids(plan),
                        "dynamic_dag": True,
                    },
                    "dynamic_plan": dyn_plan,
                    "analyzer_hints_only": True,
                    "mode": mode,
                    "scheduler": "DAGScheduler.ready_nodes_gather",
                },
                "intake",
                "Runtime Planner：动态 DAG 规格 + 波次并行执行（Adaptive Self-Correcting）。",
            ),
        },
    }

    if options.get("_web_search_blocked"):
        yield {
            "event": "step",
            "step": {
                "name": "web_search_policy",
                "status": "skipped",
                "meta": _pg(
                    {"blocked": True, "reason": options.get("_web_search_block_reason")},
                    "intake",
                    str(options.get("_web_search_block_reason") or "已禁止联网检索"),
                ),
            },
        }

    yield {
        "event": "trace",
        "trace_id": trace_id,
        "runtime": "adaptive_dag_v3",
        "phase": "intake",
        "meta": {**dict(SSE_PROTOCOL_META), "dag_runtime": True, "runtime": "adaptive_dag_v3", "phase": "intake"},
    }

    ctx = DAGRuntimeContext(harness, prompt, mode, options, messages, trace_id, hcfg, prep, _tag)
    ctx.analysis = analysis_projected
    ctx.entry_search_required = entry_search_required
    ctx.search_reason = search_reason or ""
    ctx.orch = orch
    ctx.intent = intent
    ctx.plan = plan
    ctx.intent_dict = intent_dict
    ctx.t0 = time.perf_counter()
    ctx.st = get_execution_state(options)
    ctx.budget = BudgetManager(options)
    if ctx.budget.prefer_cheaper_models(intent.latency_budget, intent.quality_requirement):
        options["_dag_cost_efficient"] = True
    if ctx.st:
        subs = [s.strip() for s in re.split(r"[？?\n]", prompt or "") if s.strip()][:12]
        ctx.st.goals = subs
        ctx.st.unresolved_goals = list(subs)
        ctx.st.resolved_goals = []
        ctx.st.risk_score = float(intent.risk_score)
        ctx.st.goal_state = GoalState(goals=subs, unresolved=list(subs), resolved=[], progress_hint=0.0).to_dict()
        ctx.st.risk_state = RiskState(
            risk_score=float(intent.risk_score),
            ambiguity=float(intent.ambiguity_score),
            high_risk_domain=bool(analysis.get("high_risk_domain")),
        ).to_dict()
        ctx.st.runtime_memory.append({"phase": "bootstrap", "intent": intent_dict})
        ctx.st.latency_budget_tier = str(intent.latency_budget or ctx.st.latency_budget_tier or "medium")
        ctx.st.quality_budget_tier = str(intent.quality_requirement or ctx.st.quality_budget_tier or "medium")
        ctx.st.set_phase("planning", node="dag_runtime_plan")

    goal_exec = options.get("_goal_execution_state")
    if not isinstance(goal_exec, GoalExecutionState):
        goal_exec = GoalExecutionState()
        options["_goal_execution_state"] = goal_exec
    if ctx.st and ctx.st.goals:
        goal_exec.goals = list(ctx.st.goals)
        goal_exec.unresolved_goals = list(ctx.st.unresolved_goals)
        goal_exec.subgoals = goal_exec.goals[: min(6, len(goal_exec.goals))]
        goal_exec.progress_score = 0.0 if goal_exec.unresolved_goals else 1.0

    ctx.caches = RuntimeTieredCaches()
    ctx.blocked = bool(options.get("_web_search_blocked"))
    ctx.overrides = {k: v for k, v in harness._search_policy_overrides("dag", analysis).items() if v is not None}

    prog = ProgressiveStreamRouter(options)
    await prog.emit_preliminary_note(intent_dict)

    dag = build_execution_dag(plan)
    async for ev in stream_scheduled_dag(ctx, dag):
        yield ev
