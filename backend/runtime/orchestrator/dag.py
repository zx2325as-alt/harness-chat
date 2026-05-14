from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set

from runtime.kernel.kernel_models import (
    NON_SUCCESS_TERMINAL_NODE_STATUSES,
    NodeStatus,
)


NodeFn = Callable[[Any], Awaitable[Dict[str, Any]]]


@dataclass
class NodeSpec:
    id: str
    deps: List[str] = field(default_factory=list)
    run: Optional[NodeFn] = None
    timeout_s: Optional[float] = None
    max_retries: int = 1
    priority: int = 100
    resource_class: str = "default"
    checkpoint_policy: str = "on_success"
    cancel_policy: str = "cooperative"


@dataclass
class NodeRuntime:
    node_id: str
    status: str = NodeStatus.PENDING
    attempt: int = 0
    enqueued_at_ms: int = 0
    started_at_ms: int = 0
    ended_at_ms: int = 0
    error: str = ""
    output_summary: Dict[str, Any] = field(default_factory=dict)


class DAG:
    """轻量依赖图：支持有向无环依赖、节点生命周期与就绪集合计算。"""

    def __init__(self) -> None:
        self.nodes: Dict[str, NodeSpec] = {}
        self.completed: Set[str] = set()
        self.failed: Dict[str, str] = {}
        self.runtime: Dict[str, NodeRuntime] = {}

    def add(self, spec: NodeSpec) -> None:
        self.nodes[spec.id] = spec
        self.runtime.setdefault(spec.id, NodeRuntime(node_id=spec.id))

    def reset_runtime(self) -> None:
        self.completed.clear()
        self.failed.clear()
        self.runtime = {nid: NodeRuntime(node_id=nid) for nid in self.nodes.keys()}

    def incomplete_ids(self) -> Set[str]:
        return {nid for nid, row in self.runtime.items() if row.status not in NON_SUCCESS_TERMINAL_NODE_STATUSES and row.status != NodeStatus.SUCCEEDED}

    def ready_nodes(self) -> List[NodeSpec]:
        out: List[NodeSpec] = []
        for nid, spec in self.nodes.items():
            row = self.runtime.get(nid)
            if not row or row.status not in (NodeStatus.PENDING, NodeStatus.READY):
                continue
            if any((self.runtime.get(dep) or NodeRuntime(dep)).status in NON_SUCCESS_TERMINAL_NODE_STATUSES for dep in spec.deps):
                continue
            if all((self.runtime.get(dep) or NodeRuntime(dep)).status == NodeStatus.SUCCEEDED for dep in spec.deps):
                out.append(spec)
        return out

    def runtime_row(self, nid: str) -> NodeRuntime:
        row = self.runtime.get(nid)
        if row is None:
            row = NodeRuntime(node_id=nid)
            self.runtime[nid] = row
        return row

    def mark_ready(self, nid: str, *, enqueued_at_ms: int = 0) -> None:
        row = self.runtime_row(nid)
        row.status = NodeStatus.READY
        if enqueued_at_ms:
            row.enqueued_at_ms = int(enqueued_at_ms)

    def mark_running(self, nid: str, *, started_at_ms: int = 0, attempt: int = 0) -> None:
        row = self.runtime_row(nid)
        row.status = NodeStatus.RUNNING
        if started_at_ms:
            row.started_at_ms = int(started_at_ms)
        if attempt:
            row.attempt = int(attempt)

    def mark_done(self, nid: str, *, ended_at_ms: int = 0, output_summary: Optional[Dict[str, Any]] = None) -> None:
        self.completed.add(nid)
        row = self.runtime_row(nid)
        row.status = NodeStatus.SUCCEEDED
        if ended_at_ms:
            row.ended_at_ms = int(ended_at_ms)
        if isinstance(output_summary, dict) and output_summary:
            row.output_summary = dict(output_summary)

    def mark_failed(self, nid: str, err: str, *, ended_at_ms: int = 0) -> None:
        self.failed[nid] = err[:1200]
        row = self.runtime_row(nid)
        row.status = NodeStatus.FAILED
        row.error = err[:1200]
        if ended_at_ms:
            row.ended_at_ms = int(ended_at_ms)

    def mark_cancelled(self, nid: str, err: str = "", *, ended_at_ms: int = 0) -> None:
        row = self.runtime_row(nid)
        row.status = NodeStatus.CANCELLED
        row.error = str(err or "")[:1200]
        if ended_at_ms:
            row.ended_at_ms = int(ended_at_ms)

    def mark_blocked(self, nid: str, err: str = "") -> None:
        row = self.runtime_row(nid)
        row.status = NodeStatus.BLOCKED
        row.error = str(err or "")[:1200]

    def terminal(self) -> bool:
        return all(row.status in (NodeStatus.SUCCEEDED, *NON_SUCCESS_TERMINAL_NODE_STATUSES) for row in self.runtime.values())
