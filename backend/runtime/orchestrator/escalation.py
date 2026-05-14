from __future__ import annotations

from typing import Any, Dict, Set


def enable_capabilities(st: Any, names: Set[str]) -> None:
    if st is None or not hasattr(st, "active_capabilities"):
        return
    cur = getattr(st, "active_capabilities")
    if isinstance(cur, set):
        cur.update(names)
    else:
        setattr(st, "active_capabilities", set(names))


def runtime_escalate_capability(options: Dict[str, Any], cap: str, *, reason: str = "") -> None:
    """能力层启用：写入 ExecutionState.active_capabilities / capability_history。"""
    st = options.get("_execution_state")
    if st is None:
        return
    enable_capabilities(st, {cap})
    if hasattr(st, "note_capability"):
        st.note_capability(cap, reason=reason)
