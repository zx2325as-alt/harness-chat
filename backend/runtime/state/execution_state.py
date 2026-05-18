"""ExecutionState / GoalExecutionState 规格源（主实现）。

runtime_state.py 从此处 re-export；所有 runtime 内部代码可直接从此模块导入。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Set

from runtime.kernel.kernel_models import NodeStatus, RunStatus, now_ts_ms


@dataclass
class ExecutionState:
    request_id: str
    prompt: str
    history_digest: str = ""
    documents_digest: str = ""
    runtime_name: str = "adaptive_dag_v3"
    run_id: str = ""
    run_status: str = RunStatus.CREATED
    started_at_ms: int = 0
    updated_at_ms: int = 0
    ended_at_ms: int = 0
    current_phase: str = "intake"
    completed_phases: List[str] = field(default_factory=list)
    phase_history: List[Dict[str, Any]] = field(default_factory=list)
    capability_history: List[str] = field(default_factory=list)
    repair_round: int = 0
    max_repair_rounds: int = 2
    repair_history: List[Dict[str, Any]] = field(default_factory=list)
    history: List[Dict[str, Any]] = field(default_factory=list)
    documents: List[Dict[str, Any]] = field(default_factory=list)
    history_tail: List[Dict[str, Any]] = field(default_factory=list)
    documents_meta: List[Dict[str, Any]] = field(default_factory=list)
    draft_answer: Optional[str] = None
    final_answer: Optional[str] = None
    search_results: List[Dict[str, Any]] = field(default_factory=list)
    search_history: List[str] = field(default_factory=list)
    search_count: int = 0
    search_budget: Optional[int] = None
    search_budget_remaining: Optional[int] = None
    quality_score: float = 0.0
    hallucination_risk: float = 0.0
    completeness_score: float = 0.0
    confidence_score: float = 0.0
    critic_issues: List[str] = field(default_factory=list)
    limitations: List[str] = field(default_factory=list)
    trace: List[Dict[str, Any]] = field(default_factory=list)
    active_capabilities: Set[str] = field(default_factory=set)
    critic_reports: List[Dict[str, Any]] = field(default_factory=list)
    verification_reports: List[Dict[str, Any]] = field(default_factory=list)
    contradictions: List[str] = field(default_factory=list)
    evidence_graph_summary: str = ""
    runtime_cost_estimate: float = 0.0
    failed_attempts: List[str] = field(default_factory=list)
    latency_budget_tier: str = "medium"
    quality_budget_tier: str = "medium"
    goals: List[str] = field(default_factory=list)
    resolved_goals: List[str] = field(default_factory=list)
    unresolved_goals: List[str] = field(default_factory=list)
    evidence_nodes: List[Dict[str, Any]] = field(default_factory=list)
    risk_score: float = 0.0
    evidence_graph_snapshot: Dict[str, Any] = field(default_factory=dict)
    runtime_memory: List[Dict[str, Any]] = field(default_factory=list)
    goal_state: Dict[str, Any] = field(default_factory=dict)
    risk_state: Dict[str, Any] = field(default_factory=dict)
    node_states: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    event_cursor: int = 0
    checkpoint_cursor: int = 0
    last_checkpoint_id: str = ""
    resume_from_run_id: str = ""
    cancel_requested: bool = False
    cancel_reason: str = ""
    terminal_error: str = ""
    public_event_count: int = 0

    @property
    def latency_budget(self) -> str:
        return self.latency_budget_tier

    @property
    def quality_budget(self) -> str:
        return self.quality_budget_tier

    @property
    def evidence_graph(self) -> Dict[str, Any]:
        return self.evidence_graph_snapshot

    @property
    def runtime_cost(self) -> float:
        try:
            return float(self.runtime_cost_estimate or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def to_public_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        # asdict() deepcopies Set without converting to list — not JSON-serializable
        d["active_capabilities"] = list(self.active_capabilities)
        d["latency_budget"] = self.latency_budget_tier
        d["quality_budget"] = self.quality_budget_tier
        d["evidence_graph"] = dict(self.evidence_graph_snapshot or {})
        d["runtime_cost"] = self.runtime_cost
        return d

    def touch(self) -> None:
        self.updated_at_ms = now_ts_ms()
        if not self.started_at_ms:
            self.started_at_ms = self.updated_at_ms

    def append_trace(self, kind: str, payload: Dict[str, Any]) -> None:
        self.touch()
        self.trace.append({"kind": kind, **payload})

    def set_phase(self, phase: str, **payload: Any) -> None:
        ph = str(phase or "").strip().lower()
        if not ph:
            return
        self.touch()
        self.current_phase = ph
        if ph not in self.completed_phases and ph != "intake":
            self.completed_phases.append(ph)
        row = {"phase": ph, "ts_ms": self.updated_at_ms}
        if payload:
            row.update(payload)
        self.phase_history.append(row)

    def note_capability(self, capability: str, *, reason: str = "") -> None:
        cap = str(capability or "").strip().lower()
        if not cap:
            return
        self.touch()
        self.active_capabilities.add(cap)
        tag = cap if not reason else f"{cap}({reason[:80]})"
        self.capability_history.append(tag)

    def note_repair_round(self, round_idx: int, **payload: Any) -> None:
        idx = max(0, int(round_idx))
        self.touch()
        self.repair_round = idx
        row = {"round": idx, "ts_ms": self.updated_at_ms}
        if payload:
            row.update(payload)
        self.repair_history.append(row)

    def _upsert_node_state(self, node_id: str, status: str, **payload: Any) -> None:
        nid = str(node_id or "").strip()
        if not nid:
            return
        self.touch()
        row = dict(self.node_states.get(nid) or {})
        row["node_id"] = nid
        row["status"] = status
        row["updated_at_ms"] = self.updated_at_ms
        if payload:
            row.update(payload)
        self.node_states[nid] = row

    def note_node_ready(self, node_id: str, **payload: Any) -> None:
        self._upsert_node_state(node_id, NodeStatus.READY, **payload)

    def note_node_started(self, node_id: str, **payload: Any) -> None:
        row = dict(self.node_states.get(str(node_id or "")) or {})
        payload.setdefault("started_at_ms", self.updated_at_ms or now_ts_ms())
        payload.setdefault("attempt", int(row.get("attempt") or 0) + 1)
        self._upsert_node_state(node_id, NodeStatus.RUNNING, **payload)

    def note_node_succeeded(self, node_id: str, **payload: Any) -> None:
        payload.setdefault("ended_at_ms", now_ts_ms())
        self._upsert_node_state(node_id, NodeStatus.SUCCEEDED, **payload)

    def note_node_failed(self, node_id: str, error: str, **payload: Any) -> None:
        payload.setdefault("ended_at_ms", now_ts_ms())
        payload["error"] = str(error or "")[:1200]
        self.failed_attempts.append(f"{node_id}:{payload['error'][:160]}")
        self._upsert_node_state(node_id, NodeStatus.FAILED, **payload)

    def note_node_cancelled(self, node_id: str, reason: str = "", **payload: Any) -> None:
        payload.setdefault("ended_at_ms", now_ts_ms())
        if reason:
            payload["error"] = str(reason)[:1200]
        self._upsert_node_state(node_id, NodeStatus.CANCELLED, **payload)

    def note_checkpoint(self, checkpoint_id: str, seq: int, **payload: Any) -> None:
        self.touch()
        self.last_checkpoint_id = str(checkpoint_id or "")
        self.checkpoint_cursor = max(int(self.checkpoint_cursor or 0), int(seq or 0))
        row = {"kind": "checkpoint", "checkpoint_id": self.last_checkpoint_id,
               "seq": self.checkpoint_cursor, "ts_ms": self.updated_at_ms}
        if payload:
            row.update(payload)
        self.runtime_memory.append(row)

    def note_public_event(self, seq: int) -> None:
        self.touch()
        self.event_cursor = max(int(self.event_cursor or 0), int(seq or 0))
        self.public_event_count = max(int(self.public_event_count or 0), int(seq or 0))

    def mark_run_terminal(self, status: str, *, error: str = "", cancel_reason: str = "") -> None:
        self.touch()
        self.run_status = str(status or self.run_status or RunStatus.FAILED)
        self.ended_at_ms = self.updated_at_ms
        if error:
            self.terminal_error = str(error)[:2000]
        if cancel_reason:
            self.cancel_requested = True
            self.cancel_reason = str(cancel_reason)[:500]


@dataclass
class GoalExecutionState:
    goals: List[str] = field(default_factory=list)
    solved_goals: List[str] = field(default_factory=list)
    unresolved_goals: List[str] = field(default_factory=list)
    evidence_map: Dict[str, Any] = field(default_factory=dict)
    attempted_actions: Set[str] = field(default_factory=set)
    search_history: List[str] = field(default_factory=list)
    progress_score: float = 0.0
    last_progress_score: float = 0.0
    progress_delta_low_rounds: int = 0
    iteration_count: int = 0
    subgoals: List[str] = field(default_factory=list)
    blocked_reasons: List[str] = field(default_factory=list)
    tool_results: Dict[str, Any] = field(default_factory=dict)


__all__ = ["ExecutionState", "GoalExecutionState"]
