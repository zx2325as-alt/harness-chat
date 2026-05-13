"""Capability Layer API：ctx.runtime.enable(\"critic\") 或 enable(options, ...) 取代互斥 track 分支。"""
from __future__ import annotations

from typing import Any, Dict

from runtime.orchestrator.escalation import runtime_escalate_capability
from runtime_state import get_execution_state


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
    st = get_execution_state(options)
    if not st:
        return {"active": [], "escalation_path": []}
    caps = sorted(st.active_capabilities) if isinstance(st.active_capabilities, set) else []
    return {"active": caps, "escalation_path": list(st.escalation_path or [])}


__all__ = ["RuntimeHandle", "enable", "enable_on_context", "snapshot_capabilities"]
