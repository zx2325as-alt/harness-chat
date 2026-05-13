from __future__ import annotations

from typing import Any, Dict, List, Optional


class SemanticMemory:
    """轻量会话侧语义记忆（不落库时可挂在 options 内）。"""

    def __init__(self) -> None:
        self.entries: List[Dict[str, Any]] = []

    def append(self, kind: str, payload: Dict[str, Any]) -> None:
        self.entries.append({"kind": kind, **payload})

    @staticmethod
    def get_or_create(options: Dict[str, Any]) -> "SemanticMemory":
        sm = options.get("_dag_semantic_memory")
        if isinstance(sm, SemanticMemory):
            return sm
        sm = SemanticMemory()
        options["_dag_semantic_memory"] = sm
        return sm


def remember_runtime_turn(options: Dict[str, Any], *, intent: Optional[Dict[str, Any]] = None, ok: bool = True) -> None:
    sm = SemanticMemory.get_or_create(options)
    sm.append("turn", {"intent": intent or {}, "ok": ok})
