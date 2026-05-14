from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional

from runtime.kernel.kernel_models import NodeStatus
from runtime.orchestrator.dag import DAG, NodeSpec

logger = logging.getLogger(__name__)


EmitFn = Callable[[Dict[str, Any]], Awaitable[None]]


class DAGScheduler:
    """任务驱动 DAG 调度：ready queue + running task map + dependency release。"""

    def __init__(self, dag: DAG, *, node_timeout_s: float = 120.0, max_retries: int = 1) -> None:
        self.dag = dag
        self.node_timeout_s = node_timeout_s
        self.max_retries = max(0, int(max_retries))

    async def run(
        self,
        ctx: Any,
        *,
        emit: Optional[EmitFn] = None,
        parallelism_metric: Optional[Callable[[int], None]] = None,
    ) -> None:
        self.dag.reset_runtime()
        ready_q: asyncio.Queue[str] = asyncio.Queue()
        running: Dict[str, asyncio.Task] = {}
        queued: set[str] = set()

        st = getattr(ctx, "st", None)
        runner = getattr(ctx, "kernel_runner", None)
        run_id = str(getattr(ctx, "run_id", "") or getattr(ctx, "trace_id", "") or "")
        trace_id = str(getattr(ctx, "trace_id", "") or run_id)

        def _note_ready(spec: NodeSpec) -> None:
            if spec.id in queued:
                return
            row = self.dag.runtime_row(spec.id)
            if row.status not in (NodeStatus.PENDING, NodeStatus.READY):
                return
            ts_ms = int(time.time() * 1000)
            self.dag.mark_ready(spec.id, enqueued_at_ms=ts_ms)
            queued.add(spec.id)
            ready_q.put_nowait(spec.id)
            if st:
                st.note_node_ready(spec.id, deps=list(spec.deps), enqueued_at_ms=ts_ms, priority=spec.priority)

        async def _publish_kernel(event_type: str, spec: NodeSpec, *, attempt: int = 0, payload: Optional[Dict[str, Any]] = None) -> None:
            if runner and run_id:
                await runner.publish_event(
                    run_id=run_id,
                    trace_id=trace_id,
                    event_type=event_type,
                    node_id=spec.id,
                    phase=str(getattr(st, "current_phase", "") or ""),
                    attempt=attempt,
                    payload=payload or {},
                )

        async def _execute(spec: NodeSpec) -> None:
            row = self.dag.runtime_row(spec.id)
            attempts = max(1, int(spec.max_retries if spec.max_retries is not None else self.max_retries) + 1)
            last_error = ""
            for attempt in range(1, attempts + 1):
                ce = getattr(ctx, "cancel_event", None)
                if ce is not None and ce.is_set():
                    ts_ms = int(time.time() * 1000)
                    self.dag.mark_cancelled(spec.id, "cancelled_before_start", ended_at_ms=ts_ms)
                    if st:
                        st.note_node_cancelled(spec.id, reason="cancelled_before_start", ended_at_ms=ts_ms)
                    await _publish_kernel("node_cancelled", spec, attempt=attempt, payload={"reason": "cancelled_before_start"})
                    return
                ts_ms = int(time.time() * 1000)
                self.dag.mark_running(spec.id, started_at_ms=ts_ms, attempt=attempt)
                if st:
                    st.note_node_started(spec.id, started_at_ms=ts_ms, attempt=attempt, deps=list(spec.deps), priority=spec.priority)
                await _publish_kernel("node_started", spec, attempt=attempt, payload={"deps": list(spec.deps)})
                try:
                    timeout_s = float(spec.timeout_s) if spec.timeout_s is not None else float(self.node_timeout_s)
                    result = await asyncio.wait_for(spec.run(ctx), timeout=timeout_s) if spec.run else None
                    end_ms = int(time.time() * 1000)
                    summary = result if isinstance(result, dict) else {}
                    self.dag.mark_done(spec.id, ended_at_ms=end_ms, output_summary=summary)
                    if st:
                        st.note_node_succeeded(spec.id, ended_at_ms=end_ms, output_summary=summary)
                    await _publish_kernel("node_succeeded", spec, attempt=attempt, payload={"output_summary": summary})
                    if runner and run_id and getattr(spec, "checkpoint_policy", "on_success") == "on_success":
                        ck = await runner.checkpoint(run_id=run_id, trace_id=trace_id, ctx=ctx, node_id=spec.id, label="node_success")
                        if st:
                            st.note_checkpoint(str(ck.get("checkpoint_id") or ""), int(ck.get("seq") or 0), node_id=spec.id)
                    return
                except asyncio.CancelledError:
                    end_ms = int(time.time() * 1000)
                    self.dag.mark_cancelled(spec.id, "cancelled", ended_at_ms=end_ms)
                    if st:
                        st.note_node_cancelled(spec.id, reason="cancelled", ended_at_ms=end_ms)
                    await _publish_kernel("node_cancelled", spec, attempt=attempt, payload={"reason": "cancelled"})
                    raise
                except Exception as e:
                    last_error = str(e)
                    if attempt < attempts:
                        logger.warning("DAG node %s retry %s/%s: %s", spec.id, attempt, attempts, e)
                        await _publish_kernel("node_retry", spec, attempt=attempt, payload={"error": last_error[:400]})
                        continue
                    end_ms = int(time.time() * 1000)
                    self.dag.mark_failed(spec.id, last_error, ended_at_ms=end_ms)
                    if st:
                        st.note_node_failed(spec.id, last_error, ended_at_ms=end_ms, attempt=attempt)
                    await _publish_kernel("node_failed", spec, attempt=attempt, payload={"error": last_error[:400]})
                    if emit:
                        await emit(
                            {
                                "event": "step",
                                "step": {
                                    "name": f"dag_node_{spec.id}",
                                    "status": "error",
                                    "error": last_error[:400],
                                },
                            }
                        )
                    return

        def _release_ready_nodes() -> None:
            for spec in self.dag.ready_nodes():
                _note_ready(spec)

        _release_ready_nodes()
        while True:
            ce = getattr(ctx, "cancel_event", None)
            if ce is not None and ce.is_set() and not running and ready_q.empty():
                logger.info("DAGScheduler: cancel_event set and runtime drained")
                break
            while not ready_q.empty():
                nid = await ready_q.get()
                queued.discard(nid)
                spec = self.dag.nodes.get(nid)
                if spec is None:
                    continue
                row = self.dag.runtime_row(nid)
                if row.status not in (NodeStatus.READY, NodeStatus.PENDING):
                    continue
                task = asyncio.create_task(_execute(spec))
                running[nid] = task
            if parallelism_metric:
                parallelism_metric(len(running))
            if not running:
                pending = self.dag.incomplete_ids()
                if not pending or self.dag.terminal():
                    break
                _release_ready_nodes()
                if ready_q.empty():
                    if self.dag.failed:
                        logger.warning("DAG stalled with failures: %s", self.dag.failed)
                    for nid in sorted(pending):
                        if self.dag.runtime_row(nid).status in (NodeStatus.PENDING, NodeStatus.READY):
                            self.dag.mark_blocked(nid, "upstream_failed_or_unresolved")
                            if st:
                                st.note_node_failed(nid, "upstream_failed_or_unresolved")
                    break
                continue
            done, _pending = await asyncio.wait(set(running.values()), return_when=asyncio.FIRST_COMPLETED)
            finished_ids = [nid for nid, task in list(running.items()) if task in done]
            for nid in finished_ids:
                task = running.pop(nid)
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            _release_ready_nodes()
