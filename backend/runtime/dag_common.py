"""DAG Runtime 共享工具：避免 dag_stream ↔ dag_phases 循环依赖。"""
from __future__ import annotations

import re
from typing import Any, Dict, List

from runtime.models.runtime_intent import RuntimeIntent
from runtime_state import get_execution_state, need_search_allowed, set_runtime_phase


def project_analysis_for_dag_runtime(analysis: Dict[str, Any]) -> Dict[str, Any]:
    """DAG 执行轨只消费运行时研判信号。"""
    if not isinstance(analysis, dict):
        return {}
    out = dict(analysis)
    out["dag_runtime_projection"] = True
    return out


def user_status(message: str, phase: str = "dag") -> Dict[str, Any]:
    return {"event": "status", "phase": phase, "message": message, "user_cognitive": True}


def sync_dag_execution_layer(options: Dict[str, Any], intent: RuntimeIntent) -> None:
    st = get_execution_state(options)
    if not st:
        return
    st.runtime_name = "adaptive_dag_v3"
    set_runtime_phase(options, "intake")
    caps = {"draft", "critic", "verify", "repair", "finalize", "planning", "reasoning"}
    if intent.search_score >= 0.25 and not options.get("_web_search_blocked"):
        caps.add("search")
    if intent.tool_requirement:
        caps.add("tool_use")
    st.active_capabilities = set(caps)
    st.latency_budget_tier = intent.latency_budget
    st.quality_budget_tier = intent.quality_requirement


def build_search_queries(
    prompt: str,
    analysis: Dict[str, Any],
    *,
    n: int,
    entry_search_required: bool,
    search_reason: str,
) -> List[str]:
    qs: List[str] = []
    base = str(prompt or "").strip().replace("\n", " ")[:220]
    if entry_search_required and search_reason:
        qs.append(str(search_reason).strip()[:400])
    if base:
        qs.append(base[:400])
    if n >= 2 and base:
        qs.append(f"{base[:180]} 最新 要点")
    if n >= 3 and base:
        qs.append(f"{base[:180]} 官方 来源")
    out: List[str] = []
    for q in qs:
        q = str(q).strip()
        if q and q not in out:
            out.append(q)
    return out[: max(0, n)]
