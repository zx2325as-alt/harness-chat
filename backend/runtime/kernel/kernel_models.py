from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


class RunStatus:
    CREATED = "created"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    RESUMABLE = "resumable"


class NodeStatus:
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"
    BLOCKED = "blocked"


TERMINAL_NODE_STATUSES = {
    NodeStatus.SUCCEEDED,
    NodeStatus.FAILED,
    NodeStatus.CANCELLED,
    NodeStatus.SKIPPED,
    NodeStatus.BLOCKED,
}


NON_SUCCESS_TERMINAL_NODE_STATUSES = {
    NodeStatus.FAILED,
    NodeStatus.CANCELLED,
    NodeStatus.BLOCKED,
}


def now_ts_ms() -> int:
    return int(time.time() * 1000)


@dataclass
class KernelNodeSpec:
    id: str
    deps: List[str] = field(default_factory=list)
    timeout_s: Optional[float] = None
    max_retries: int = 1
    priority: int = 100
    resource_class: str = "default"
    checkpoint_policy: str = "on_success"
    cancel_policy: str = "cooperative"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "deps": list(self.deps),
            "timeout_s": self.timeout_s,
            "max_retries": self.max_retries,
            "priority": self.priority,
            "resource_class": self.resource_class,
            "checkpoint_policy": self.checkpoint_policy,
            "cancel_policy": self.cancel_policy,
        }


@dataclass
class KernelNodeRuntime:
    node_id: str
    status: str = NodeStatus.PENDING
    deps: List[str] = field(default_factory=list)
    attempt: int = 0
    priority: int = 100
    enqueued_at_ms: int = 0
    started_at_ms: int = 0
    ended_at_ms: int = 0
    error: str = ""
    retryable: bool = True
    checkpointed: bool = False
    output_summary: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "node_id": self.node_id,
            "status": self.status,
            "deps": list(self.deps),
            "attempt": self.attempt,
            "priority": self.priority,
            "enqueued_at_ms": self.enqueued_at_ms,
            "started_at_ms": self.started_at_ms,
            "ended_at_ms": self.ended_at_ms,
            "error": self.error,
            "retryable": bool(self.retryable),
            "checkpointed": bool(self.checkpointed),
            "output_summary": dict(self.output_summary or {}),
        }


@dataclass
class KernelRunSpec:
    run_id: str
    trace_id: str
    runtime_name: str
    nodes: List[KernelNodeSpec] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "trace_id": self.trace_id,
            "runtime_name": self.runtime_name,
            "nodes": [node.to_dict() for node in self.nodes],
            "metadata": dict(self.metadata or {}),
        }


@dataclass
class KernelEvent:
    run_id: str
    trace_id: str
    event_type: str
    seq: int = 0
    ts_ms: int = field(default_factory=now_ts_ms)
    node_id: str = ""
    phase: str = ""
    attempt: int = 0
    payload: Dict[str, Any] = field(default_factory=dict)
    public_event: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "trace_id": self.trace_id,
            "event_type": self.event_type,
            "seq": self.seq,
            "ts_ms": self.ts_ms,
            "node_id": self.node_id,
            "phase": self.phase,
            "attempt": self.attempt,
            "payload": dict(self.payload or {}),
            "public_event": dict(self.public_event or {}) if isinstance(self.public_event, dict) else None,
        }


@dataclass
class CheckpointRef:
    run_id: str
    checkpoint_id: str
    seq: int
    ts_ms: int
    node_id: str = ""
    label: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "checkpoint_id": self.checkpoint_id,
            "seq": self.seq,
            "ts_ms": self.ts_ms,
            "node_id": self.node_id,
            "label": self.label,
        }
