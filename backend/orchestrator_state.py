"""
Capability Planner + ExecutionState 引导启动。
执行状态真相源：runtime_state.ExecutionState.current_phase / active_capabilities。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from runtime_state import ExecutionState
from runtime.kernel.kernel_models import RunStatus, now_ts_ms

__all__ = [
    "ExecutionState",
    "apply_capability_planner",
    "bootstrap_execution_state",
]


def _risk_level_from_analysis(analysis: Dict[str, Any]) -> str:
    if analysis.get("high_risk_domain"):
        return "high"
    if analysis.get("numeric_sensitive") or analysis.get("source_sensitive"):
        return "medium-high"
    if str(analysis.get("complexity") or "low").lower() == "high":
        return "medium"
    return "low"


def _search_policy_from_analysis(analysis: Dict[str, Any]) -> str:
    si = str(analysis.get("search_intent") or "none").lower()
    if si in ("required", "freshness_required"):
        return "required"
    if si == "explicit":
        return "explicit"
    if analysis.get("search_required"):
        return "suggested"
    return "optional"


def _compact_history_tail(messages: Optional[List[Dict[str, Any]]], max_turns: int = 8) -> List[Dict[str, Any]]:
    if not messages:
        return []
    out: List[Dict[str, Any]] = []
    for m in messages[-max_turns:]:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        if role not in ("user", "assistant"):
            continue
        c = m.get("content")
        text = c if isinstance(c, str) else str(c or "")[:400]
        out.append({"role": role, "chars": len(text), "preview": text[:160]})
    return out


def _history_turns_compact(messages: Optional[List[Dict[str, Any]]], max_turns: int = 12, max_chars: int = 2000) -> List[Dict[str, Any]]:
    """ExecutionState.history：保留角色与截断正文（与 digest 互补）。"""
    if not messages:
        return []
    out: List[Dict[str, Any]] = []
    for m in messages[-max_turns:]:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        if role not in ("user", "assistant", "system"):
            continue
        c = m.get("content")
        text = c if isinstance(c, str) else str(c or "")
        out.append({"role": role, "content": text[:max_chars], "chars": len(text)})
    return out


def _documents_snapshot(documents: Any, *, limit: int = 24) -> List[Dict[str, Any]]:
    if not isinstance(documents, list):
        return []
    out: List[Dict[str, Any]] = []
    keys = ("name", "id", "status", "type", "mime", "size", "uri")
    for d in documents[:limit]:
        if isinstance(d, dict):
            out.append({k: d.get(k) for k in keys if k in d and d.get(k) is not None})
    return out


def _compact_documents_meta(documents: Any) -> List[Dict[str, Any]]:
    if not isinstance(documents, list):
        return []
    out: List[Dict[str, Any]] = []
    for d in documents[:16]:
        if isinstance(d, dict):
            out.append({"name": d.get("name"), "status": d.get("status")})
    return out


def apply_capability_planner(analysis: Dict[str, Any]) -> Dict[str, Any]:
    """
    Capability Planner：产出 capability_plan（能力与检索策略），供 DAG Runtime 节点消费。
    运行时统一由 Adaptive DAG Runtime 执行，不再维护互斥 track 真相源。
    """
    cx = str(analysis.get("complexity") or "low").lower()
    if cx not in ("low", "medium", "high"):
        cx = "medium"
    plan = {
        "capability_level": cx,
        "response_style": str(analysis.get("response_style") or "normal").lower(),
        "search_policy": _search_policy_from_analysis(analysis),
        "risk_level": _risk_level_from_analysis(analysis),
    }
    analysis["capability_plan"] = plan
    return plan


def bootstrap_execution_state(
    trace_id: str,
    prompt: str,
    analysis: Dict[str, Any],
    options: Dict[str, Any],
    *,
    max_repair_rounds: int = 2,
    messages: Optional[List[Dict[str, Any]]] = None,
) -> ExecutionState:
    lims = list(analysis.get("limitations") or [])
    hist = str(options.get("_history_signature") or "")[:4000]
    docs = str(options.get("_documents_signature") or "")[:4000]
    bud = options.get("_search_budget_remaining")
    st = ExecutionState(
        request_id=trace_id,
        run_id=str(options.get("run_id") or trace_id),
        run_status=RunStatus.RUNNING,
        started_at_ms=now_ts_ms(),
        updated_at_ms=now_ts_ms(),
        prompt=(prompt or "")[:8000],
        history_digest=hist,
        documents_digest=docs,
        history=_history_turns_compact(messages),
        documents=_documents_snapshot(options.get("documents")),
        history_tail=_compact_history_tail(messages),
        documents_meta=_compact_documents_meta(options.get("documents")),
        limitations=lims,
        search_budget=bud if isinstance(bud, int) else None,
        search_budget_remaining=bud if isinstance(bud, int) else None,
        max_repair_rounds=max(1, min(8, int(max_repair_rounds))),
    )
    st.set_phase("intake", bootstrap=True)
    options["_execution_state"] = st
    options["_runtime_name"] = st.runtime_name
    options["_runtime_phase"] = st.current_phase
    return st
