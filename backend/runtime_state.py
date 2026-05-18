"""动态 Runtime 真相源：类实现已迁移至 runtime/state/execution_state.py；此文件保留工具函数并 re-export 类。"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

# 规格源（单一实现）
from runtime.state.execution_state import ExecutionState, GoalExecutionState  # noqa: F401

__all__ = [
    "ExecutionState",
    "GoalExecutionState",
    "get_execution_state",
    "execution_evidence_context",
    "append_search_evidence_rows",
    "runtime_phase",
    "set_runtime_phase",
    "sync_execution_search_budget",
    "need_search_allowed",
    "need_search",
    "note_search_consumed",
]


def get_execution_state(options: Dict[str, Any]) -> Optional[ExecutionState]:
    st = options.get("_execution_state")
    return st if isinstance(st, ExecutionState) else None


def execution_evidence_context(options: Dict[str, Any], *, max_chars: int = 8000) -> str:
    """供 Fast / Unified Critic 注入的已检索证据摘录（ExecutionState.search_results）。"""
    st = get_execution_state(options)
    if not st or not st.search_results:
        return ""
    parts: List[str] = []
    for row in st.search_results[-20:]:
        if not isinstance(row, dict):
            continue
        src = str(row.get("source") or "").strip()
        body = str(row.get("content") or "").strip()
        if not body:
            continue
        head = f"[{src}]\n" if src else ""
        parts.append(f"{head}{body[:2400]}")
    blob = "\n\n".join(parts)
    return blob[:max_chars] if len(blob) > max_chars else blob


def append_search_evidence_rows(options: Dict[str, Any], rows: List[Dict[str, Any]]) -> None:
    st = get_execution_state(options)
    if not st or not rows:
        return
    for r in rows[-24:]:
        if isinstance(r, dict):
            st.search_results.append(r)


def runtime_phase(options: Dict[str, Any]) -> str:
    st = get_execution_state(options)
    if st:
        return str(st.current_phase or "intake").strip().lower()
    return str(options.get("_runtime_phase") or "intake").strip().lower()


def set_runtime_phase(options: Dict[str, Any], phase: str) -> None:
    ph = str(phase or "intake").strip().lower()
    options["_runtime_phase"] = ph
    st = get_execution_state(options)
    if st:
        st.set_phase(ph)


def sync_execution_search_budget(options: Dict[str, Any]) -> None:
    st = get_execution_state(options)
    if not st:
        return
    bud = options.get("_search_budget_remaining")
    st.search_budget_remaining = bud if isinstance(bud, int) else None


def need_search_allowed(options: Dict[str, Any]) -> bool:
    """Runtime 侧是否还允许发起「计费」检索（无预算配置时视为允许）。"""
    bud = options.get("_search_budget_remaining")
    if bud is None:
        return True
    try:
        return int(bud) > 0
    except (TypeError, ValueError):
        return True


# 文档第七章命名别名
need_search = need_search_allowed


def note_search_consumed(options: Dict[str, Any]) -> None:
    st = get_execution_state(options)
    if st:
        st.search_count += 1
    sync_execution_search_budget(options)
