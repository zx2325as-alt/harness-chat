"""Runtime-Centric 共享上下文：节点通过 ctx.emit 推送 SSE 事件。"""
from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set

from runtime.capability_runtime import RuntimeHandle
from runtime.kernel.in_memory_store import InMemoryKernelStore
from runtime.kernel.kernel_events import KernelEventPublisher
from runtime.kernel.kernel_runner import KernelRunner


class DAGRuntimeContext:
    """供 DAG 节点共享的可变上下文（Async DAG + Self-Correcting 主路径）。"""

    def __init__(
        self,
        harness: Any,
        prompt: str,
        mode: str,
        options: Dict[str, Any],
        messages: Optional[List[Dict[str, Any]]],
        trace_id: str,
        hcfg: Dict[str, Any],
        prep: Dict[str, Any],
        _tag: Callable[..., Dict[str, Any]],
    ) -> None:
        self.harness = harness
        self.prompt = prompt
        self.mode = mode
        self.options = options
        self.messages = messages
        self.trace_id = trace_id
        self.run_id = str(options.get("run_id") or trace_id)
        self.hcfg = hcfg
        self.prep = prep
        self._tag = _tag

        async def _emit_unbound(_ev: Dict[str, Any]) -> None:
            raise RuntimeError("DAGRuntimeContext.emit：应由 stream_scheduled_dag 注入后再调度节点")

        self.emit: Callable[[Dict[str, Any]], Awaitable[None]] = _emit_unbound

        self.analysis: Dict[str, Any] = {}
        self.entry_search_required = False
        self.search_reason = ""
        self.orch: Any = None
        self.intent: Any = None
        self.plan: Any = None
        self.intent_dict: Dict[str, Any] = {}
        self.t0 = 0.0
        self.st: Any = None
        self.budget: Any = None
        self.blocked = False
        self.overrides: Dict[str, Any] = {}
        self.search_pairs: List[Any] = []
        self.evidence_objs: List[Any] = []
        self.ev_text = ""
        self.evidence_graph: Any = None
        self.quality_ctx: Dict[str, Any] = {}
        self.chain_on = True
        self.default_model = ""
        self.quality_models: Dict[str, Any] = {}
        self.review_cands: List[str] = []
        self.repair_pool: List[str] = []
        self.history_chars = 4000
        self.use_quality_layers = False
        self.base_prompt = ""
        self.draft_candidates: List[str] = []
        self.draft_messages: Any = None
        self.draft = ""
        self.max_wave_parallel = 1
        self.should_stop = False
        self.caches: Any = None
        raw_ce = options.get("_dag_cancel_event")
        self.cancel_event: asyncio.Event = raw_ce if isinstance(raw_ce, asyncio.Event) else asyncio.Event()
        self.runtime = RuntimeHandle(options)
        self.kernel_store = options.get("_kernel_store") if options.get("_kernel_store") is not None else InMemoryKernelStore()
        self.kernel_publisher = options.get("_kernel_publisher") if options.get("_kernel_publisher") is not None else KernelEventPublisher()
        self.kernel_runner = options.get("_kernel_runner") if options.get("_kernel_runner") is not None else KernelRunner(store=self.kernel_store, publisher=self.kernel_publisher)
        self._quality_done = False
        try:
            self.dag_node_max_retries = max(0, int(options.get("_dag_node_max_retries", 1)))
        except (TypeError, ValueError):
            self.dag_node_max_retries = 1

        self._critic_unified_by_round: Dict[int, Any] = {}
        self._critic_facets_by_round: Dict[int, Dict[str, Any]] = {}
        self._critic_struct_by_round: Dict[int, Any] = {}
        self._critic_merged_by_round: Dict[int, Dict[str, Any]] = {}
        self._skip_critic_wave: Dict[int, bool] = {}
        self._round_budget_aborted: Set[int] = set()

    def enable_capability(self, name: str, *, reason: str = "") -> None:
        self.runtime.enable(name, reason=reason)
