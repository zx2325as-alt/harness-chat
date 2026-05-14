"""DAG Runtime 执行桥：Planner DAG -> task-driven scheduler -> kernel event log -> public stream projection。"""
from __future__ import annotations

import asyncio
from typing import Any, AsyncGenerator, Dict, List, TYPE_CHECKING

from runtime.kernel.kernel_models import RunStatus
from runtime.kernel.kernel_projectors import event_to_public_event
from runtime.orchestrator.dag import DAG, NodeSpec
from runtime.orchestrator.scheduler import DAGScheduler

if TYPE_CHECKING:
    from runtime.orchestrator.runtime_planner import PlanDescription

_SENTINEL = object()


def planned_dag_node_ids(plan: "PlanDescription") -> List[str]:
    """与 build_execution_dag 一致的节点 ID 序列（供 Planner SSE 元数据）。"""
    ids: List[str] = ["parallel_search", "parallel_draft"]
    if plan.use_tool_gate:
        ids.append("tool_capability_gate")
    if plan.use_goal_subgraph:
        ids.append("goal_capability_gate")
    layered = plan.layered_critics
    dual = plan.parallel_critics and not layered
    mono = not layered and not dual
    for i in range(plan.repair_rounds_max):
        if mono:
            ids.append(f"quality_round_{i}")
            continue
        ids.append(f"quality_prelude_{i}")
        if layered:
            ids.append(f"critic_unified_{i}")
            for fk in ("coverage", "logic", "evidence", "hallucination", "policy"):
                ids.append(f"critic_facet_{i}_{fk}")
            ids.append(f"critic_merge_{i}")
        else:
            ids.append(f"critic_unified_{i}")
            ids.append(f"critic_structured_{i}")
            ids.append(f"critic_merge_{i}")
        ids.extend([f"search_followup_{i}", f"repair_round_{i}", f"verify_round_{i}"])
    ids.append("finalize_output")
    return ids


def build_execution_dag(plan: "PlanDescription") -> DAG:
    """Planner 动态 DAG：layered 时 unified+5 facet；paired 时 unified+structured；否则单节点 quality 轮。"""
    from runtime.nodes import dag_phases

    dag = DAG()
    dag.add(NodeSpec("parallel_search", deps=[], run=dag_phases.node_parallel_search))
    dag.add(NodeSpec("parallel_draft", deps=["parallel_search"], run=dag_phases.node_parallel_draft))
    prev = "parallel_draft"
    if plan.use_tool_gate:
        dag.add(NodeSpec("tool_capability_gate", deps=[prev], run=dag_phases.node_tool_capability_gate))
        prev = "tool_capability_gate"
    if plan.use_goal_subgraph:
        dag.add(NodeSpec("goal_capability_gate", deps=[prev], run=dag_phases.node_goal_capability_gate))
        prev = "goal_capability_gate"

    layered = plan.layered_critics
    dual = plan.parallel_critics and not layered
    mono = not layered and not dual

    for i in range(plan.repair_rounds_max):
        if mono:
            nid = f"quality_round_{i}"
            dag.add(NodeSpec(nid, deps=[prev], run=dag_phases.make_quality_round_runner(i)))
            prev = nid
            continue
        prelude = f"quality_prelude_{i}"
        dag.add(NodeSpec(prelude, deps=[prev], run=dag_phases.make_prelude_runner(i)))
        if layered:
            uni = f"critic_unified_{i}"
            dag.add(NodeSpec(uni, deps=[prelude], run=dag_phases.make_critic_unified_runner(i)))
            facet_ids: List[str] = []
            for fk in ("coverage", "logic", "evidence", "hallucination", "policy"):
                fid = f"critic_facet_{i}_{fk}"
                dag.add(NodeSpec(fid, deps=[prelude], run=dag_phases.make_critic_facet_runner(i, fk)))
                facet_ids.append(fid)
            merge = f"critic_merge_{i}"
            dag.add(NodeSpec(merge, deps=[uni] + facet_ids, run=dag_phases.make_critic_merge_layered_runner(i)))
            tail = merge
        else:
            u = f"critic_unified_{i}"
            s = f"critic_structured_{i}"
            dag.add(NodeSpec(u, deps=[prelude], run=dag_phases.make_critic_paired_unified_runner(i)))
            dag.add(NodeSpec(s, deps=[prelude], run=dag_phases.make_critic_paired_structured_runner(i)))
            m2 = f"critic_merge_{i}"
            dag.add(NodeSpec(m2, deps=[u, s], run=dag_phases.make_critic_merge_paired_runner(i)))
            tail = m2
        sf = f"search_followup_{i}"
        dag.add(NodeSpec(sf, deps=[tail], run=dag_phases.make_search_followup_runner(i)))
        rep = f"repair_round_{i}"
        dag.add(NodeSpec(rep, deps=[sf], run=dag_phases.make_repair_round_dag_runner(i)))
        ver = f"verify_round_{i}"
        dag.add(NodeSpec(ver, deps=[rep], run=dag_phases.make_verify_round_dag_runner(i)))
        prev = ver

    dag.add(NodeSpec("finalize_output", deps=[prev], run=dag_phases.node_finalize_output))
    return dag


def build_main_execution_dag() -> DAG:
    """兼容默认 Plan（测试/旧调用）；生产路径请使用 build_execution_dag(plan)。"""
    from runtime.orchestrator.runtime_planner import PlanDescription

    return build_execution_dag(
        PlanDescription(
            parallel_searches=2,
            parallel_critics=True,
            repair_rounds_max=2,
            use_goal_subgraph=False,
            use_tool_gate=False,
            parallel_drafts=True,
            hedge_draft_delay_ms=0,
            layered_critics=True,
        )
    )


async def stream_scheduled_dag(ctx: Any, dag: DAG, *, node_timeout_s: float = 180.0) -> AsyncGenerator[Dict[str, Any], None]:
    from runtime.streaming.chunk_router import augment_streaming_chunk
    from runtime_state import set_runtime_phase

    q: asyncio.Queue = asyncio.Queue()
    runner = ctx.kernel_runner
    publisher = ctx.kernel_publisher
    run_id = str(ctx.run_id or ctx.trace_id)
    trace_id = str(ctx.trace_id or run_id)
    st = getattr(ctx, "st", None)

    async def emit(ev: Dict[str, Any]) -> None:
        if isinstance(ev, dict):
            if ev.get("event") == "chunk":
                ev = augment_streaming_chunk(ev)
            elif ev.get("event") == "status" and ev.get("phase"):
                set_runtime_phase(ctx.options, str(ev.get("phase") or "intake"))
            elif ev.get("event") == "step" and isinstance(ev.get("step"), dict):
                meta = ev["step"].get("meta") if isinstance(ev["step"].get("meta"), dict) else {}
                phase = meta.get("phase_group") or meta.get("pipeline_phase")
                if phase:
                    set_runtime_phase(ctx.options, str(phase))
        seq = await runner.publish_legacy_public_event(run_id=run_id, trace_id=trace_id, event=ev, phase=str(getattr(st, "current_phase", "") or ""))
        if st:
            st.note_public_event(seq)

    ctx.emit = emit
    sched = DAGScheduler(dag, node_timeout_s=node_timeout_s, max_retries=getattr(ctx, "dag_node_max_retries", 1))

    sub_q = publisher.subscribe()
    await runner.start_run(
        run_id=run_id,
        trace_id=trace_id,
        payload={
            "runtime": str(ctx.options.get("_runtime_name") or "adaptive_dag_v3"),
            "trace_id": trace_id,
            "node_ids": list(dag.nodes.keys()),
        },
    )

    async def fanout() -> None:
        while True:
            item = await sub_q.get()
            if item is _SENTINEL:
                break
            public_event = event_to_public_event(item)
            if isinstance(public_event, dict):
                if st and public_event.get("event_seq"):
                    st.note_public_event(int(public_event.get("event_seq") or 0))
                await q.put(public_event)

    fan_task = asyncio.create_task(fanout())

    async def runner_task() -> None:
        try:
            async def err_emit(ev: Dict[str, Any]) -> None:
                await emit(ev)

            await sched.run(ctx, emit=err_emit)
            has_failures = bool(getattr(dag, "failed", {}))
            cancelled = bool(isinstance(getattr(ctx, "cancel_event", None), asyncio.Event) and ctx.cancel_event.is_set() and not has_failures)
            if has_failures:
                err_text = "; ".join(f"{nid}:{msg}" for nid, msg in list(getattr(dag, "failed", {}).items())[:8]) or "dag_failed"
                if st:
                    st.mark_run_terminal(RunStatus.FAILED, error=err_text)
                await runner.fail_run(run_id=run_id, trace_id=trace_id, error=err_text, payload={"phase": str(getattr(st, "current_phase", "") or "")})
            elif cancelled:
                if st:
                    st.mark_run_terminal(RunStatus.CANCELLED, cancel_reason="cancel_event_set")
                await runner.cancel_run(run_id=run_id, trace_id=trace_id, error="cancel_event_set")
            else:
                if st:
                    st.mark_run_terminal(RunStatus.COMPLETED)
                await runner.complete_run(run_id=run_id, trace_id=trace_id, payload={"phase": str(getattr(st, "current_phase", "") or "")})
        except asyncio.CancelledError:
            if st:
                st.mark_run_terminal(RunStatus.CANCELLED, cancel_reason="scheduler_cancelled")
            await runner.cancel_run(run_id=run_id, trace_id=trace_id, error="scheduler_cancelled")
            raise
        except Exception as exc:
            if st:
                st.mark_run_terminal(RunStatus.FAILED, error=str(exc))
            await runner.fail_run(run_id=run_id, trace_id=trace_id, error=str(exc))
            raise
        finally:
            await publisher.close(_SENTINEL)
            await fan_task
            await q.put(_SENTINEL)

    task = asyncio.create_task(runner_task())
    try:
        while True:
            ev = await q.get()
            if ev is _SENTINEL:
                break
            yield ev
    finally:
        ce = getattr(ctx, "cancel_event", None)
        if isinstance(ce, asyncio.Event) and not ce.is_set():
            ce.set()
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if not fan_task.done():
            fan_task.cancel()
            try:
                await fan_task
            except asyncio.CancelledError:
                pass
