"""Runtime State Graph：DAG 节点完成/失败视图（供调试与 Planner 扩展）。"""
from __future__ import annotations

from typing import Dict, Set


class RuntimeGraphView:
    def __init__(self) -> None:
        self.completed: Set[str] = set()
        self.failed: Dict[str, str] = {}

    def mark_done(self, node_id: str) -> None:
        self.completed.add(node_id)

    def mark_failed(self, node_id: str, err: str) -> None:
        self.failed[node_id] = err[:800]
