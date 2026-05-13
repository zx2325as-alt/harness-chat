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
    """能力级「升级」：写入 ExecutionState.escalation_path（字符串路径）。"""
    st = options.get("_execution_state")
    if st is None:
        return
    enable_capabilities(st, {cap})
    tag = f"cap:{cap}"
    if reason:
        tag += f"({reason[:80]})"
    if hasattr(st, "escalation_path") and isinstance(st.escalation_path, list):
        st.escalation_path.append(tag)
    if hasattr(st, "escalation_count"):
        try:
            st.escalation_count = int(st.escalation_count or 0) + 1
        except (TypeError, ValueError):
            st.escalation_count = 1
