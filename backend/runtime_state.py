"""动态 Runtime 真相源：ExecutionState / AgentState（与文档对齐）。"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Set

# 单调升级秩；dag 为当前默认执行轨（最高）。
TRACK_RANK: Dict[str, int] = {"fast": 0, "refine": 1, "agent": 2, "dag": 3}


def track_rank_value(track: str) -> int:
    t = str(track or "fast").strip().lower()
    return TRACK_RANK.get(t, 0)


def is_strict_track_upgrade(from_track: str, to_track: str) -> bool:
    return track_rank_value(to_track) > track_rank_value(from_track)


@dataclass
class ExecutionState:
    request_id: str
    prompt: str
    history_digest: str = ""
    documents_digest: str = ""
    """近期用户/助手轮次（截断正文，供 Runtime 调试与追溯；大上下文仍以 digest 为准）。"""
    history: List[Dict[str, Any]] = field(default_factory=list)
    """上传文档快照（元数据为主，避免把全文塞进 state）。"""
    documents: List[Dict[str, Any]] = field(default_factory=list)
    """近期对话摘要（仅元数据，控制体积；与 history_digest 并存）。"""
    history_tail: List[Dict[str, Any]] = field(default_factory=list)
    """上传文档摘要（文件名/状态等）。"""
    documents_meta: List[Dict[str, Any]] = field(default_factory=list)
    initial_track: str = "dag"
    current_track: str = "dag"
    draft_answer: Optional[str] = None
    final_answer: Optional[str] = None
    search_results: List[Dict[str, Any]] = field(default_factory=list)
    """本轮发出的检索查询历史（query 文本，可观测 / Debug）。"""
    search_history: List[str] = field(default_factory=list)
    search_count: int = 0
    """本轮请求检索次数上限（与 search_budget_remaining 初始一致；None 表示不限制）。"""
    search_budget: Optional[int] = None
    search_budget_remaining: Optional[int] = None
    quality_score: float = 0.0
    hallucination_risk: float = 0.0
    completeness_score: float = 0.0
    confidence_score: float = 0.0
    critic_issues: List[str] = field(default_factory=list)
    limitations: List[str] = field(default_factory=list)
    escalation_count: int = 0
    max_escalations: int = 2
    escalation_path: List[str] = field(default_factory=list)
    trace: List[Dict[str, Any]] = field(default_factory=list)
    # DAG / 能力化 Runtime 扩展（默认执行轨为 dag；秩比较仍兼容历史 fast|refine|agent 字符串）
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
    # evidence_graph_snapshot：EvidenceGraph.to_dict()；runtime_memory：失败路径/修复轨迹等
    evidence_graph_snapshot: Dict[str, Any] = field(default_factory=dict)
    runtime_memory: List[Dict[str, Any]] = field(default_factory=list)
    goal_state: Dict[str, Any] = field(default_factory=dict)
    risk_state: Dict[str, Any] = field(default_factory=dict)

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
        d["latency_budget"] = self.latency_budget_tier
        d["quality_budget"] = self.quality_budget_tier
        d["evidence_graph"] = dict(self.evidence_graph_snapshot or {})
        d["runtime_cost"] = self.runtime_cost
        return d

    def append_trace(self, kind: str, payload: Dict[str, Any]) -> None:
        self.trace.append({"kind": kind, **payload})


@dataclass
class AgentState:
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


def get_execution_state(options: Dict[str, Any]) -> Optional[ExecutionState]:
    st = options.get("_execution_state")
    return st if isinstance(st, ExecutionState) else None


def execution_evidence_context(options: Dict[str, Any], *, max_chars: int = 8000) -> str:
    """供 Fast / Unified Critic 注入的已检索证据摘录（ExecutionState.search_results）。"""
    st = get_execution_state(options)
    if not st or not st.search_results:
        return ""
    parts: List[str] = []
    for row in st.search_results[-20:]:
        if not isinstance(row, dict):
            continue
        src = str(row.get("source") or "").strip()
        body = str(row.get("content") or "").strip()
        if not body:
            continue
        head = f"[{src}]\n" if src else ""
        parts.append(f"{head}{body[:2400]}")
    blob = "\n\n".join(parts)
    return blob[:max_chars] if len(blob) > max_chars else blob


def append_search_evidence_rows(options: Dict[str, Any], rows: List[Dict[str, Any]]) -> None:
    st = get_execution_state(options)
    if not st or not rows:
        return
    for r in rows[-24:]:
        if isinstance(r, dict):
            st.search_results.append(r)


def runtime_track(options: Dict[str, Any]) -> str:
    st = get_execution_state(options)
    if st:
        return str(st.current_track or "dag").strip().lower()
    return str(options.get("_runtime_track") or "dag").strip().lower()


def set_runtime_track(options: Dict[str, Any], track: str) -> None:
    """与 escalate 一致：仅允许单调升级，禁止降级写入。"""
    t = str(track or "dag").strip().lower()
    st = get_execution_state(options)
    cur = runtime_track(options)
    if st and t != cur and not is_strict_track_upgrade(cur, t):
        return
    options["_runtime_track"] = t
    if st:
        st.current_track = t


def sync_execution_search_budget(options: Dict[str, Any]) -> None:
    st = get_execution_state(options)
    if not st:
        return
    bud = options.get("_search_budget_remaining")
    st.search_budget_remaining = bud if isinstance(bud, int) else bud


def need_search_allowed(options: Dict[str, Any]) -> bool:
    """Runtime 侧是否还允许发起「计费」检索（无预算配置时视为允许）。"""
    bud = options.get("_search_budget_remaining")
    if bud is None:
        return True
    try:
        return int(bud) > 0
    except (TypeError, ValueError):
        return True


# 文档第七章命名别名
need_search = need_search_allowed


def note_search_consumed(options: Dict[str, Any]) -> None:
    st = get_execution_state(options)
    if st:
        st.search_count += 1
    sync_execution_search_budget(options)
