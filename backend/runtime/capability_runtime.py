"""Capability Layer API：ctx.runtime.enable(\"critic\") 或 enable(options, ...) 取代互斥 track 分支。"""
from __future__ import annotations

from typing import Any, Dict

from runtime.orchestrator.escalation import runtime_escalate_capability


class RuntimeHandle:
    """绑定单请求的 options，支持文档式调用 runtime.enable(\"search\")。"""

    __slots__ = ("_options",)

    def __init__(self, options: Dict[str, Any]) -> None:
        self._options = options

    def enable(self, capability: str, *, reason: str = "") -> None:
        runtime_escalate_capability(self._options, capability, reason=reason)


def enable(options: Dict[str, Any], capability: str, *, reason: str = "") -> None:
    runtime_escalate_capability(options, capability, reason=reason)


def enable_on_context(ctx: Any, capability: str, *, reason: str = "") -> None:
    enable(ctx.options, capability, reason=reason)


def snapshot_capabilities(options: Dict[str, Any]) -> Dict[str, Any]:
    st = options.get("_execution_state")
    if st is None or not hasattr(st, "active_capabilities"):
        return {"active": [], "history": []}
    caps = sorted(st.active_capabilities) if isinstance(st.active_capabilities, set) else []
    history = list(getattr(st, "capability_history", []) or [])
    return {"active": caps, "history": history}


__all__ = ["RuntimeHandle", "enable", "enable_on_context", "snapshot_capabilities"]
