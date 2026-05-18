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


def remember_runtime_turn(
    options: Dict[str, Any],
    *,
    intent: Optional[Dict[str, Any]] = None,
    ok: bool = True,
    repair_rounds: int = 0,
    search_count: int = 0,
) -> None:
    sm = SemanticMemory.get_or_create(options)
    payload: Dict[str, Any] = {"intent": intent or {}, "ok": ok}
    if repair_rounds:
        payload["repair_rounds"] = repair_rounds
    if search_count:
        payload["search_count"] = search_count
    sm.append("turn", payload)
    # 控制内存大小：保留最近 32 条轮次记录
    turns = [e for e in sm.entries if e.get("kind") == "turn"]
    if len(turns) > 32:
        sm.entries = [e for e in sm.entries if e.get("kind") != "turn"] + turns[-32:]
