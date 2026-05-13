from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set


NodeFn = Callable[[Any], Awaitable[Dict[str, Any]]]


@dataclass
class NodeSpec:
    id: str
    deps: List[str] = field(default_factory=list)
    run: Optional[NodeFn] = None


class DAG:
    """轻量依赖图：仅支持有向无环依赖与就绪集合计算。"""

    def __init__(self) -> None:
        self.nodes: Dict[str, NodeSpec] = {}
        self.completed: Set[str] = set()
        self.failed: Dict[str, str] = {}

    def add(self, spec: NodeSpec) -> None:
        self.nodes[spec.id] = spec

    def reset_runtime(self) -> None:
        self.completed.clear()
        self.failed.clear()

    def incomplete_ids(self) -> Set[str]:
        return set(self.nodes.keys()) - self.completed - set(self.failed.keys())

    def ready_nodes(self) -> List[NodeSpec]:
        out: List[NodeSpec] = []
        for nid, spec in self.nodes.items():
            if nid in self.completed or nid in self.failed:
                continue
            if all(d in self.completed for d in spec.deps):
                out.append(spec)
        return out

    def mark_done(self, nid: str) -> None:
        self.completed.add(nid)

    def mark_failed(self, nid: str, err: str) -> None:
        self.failed[nid] = err[:1200]
