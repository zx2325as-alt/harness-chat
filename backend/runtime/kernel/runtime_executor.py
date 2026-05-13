"""DAGScheduler + 事件队列：就绪波次 asyncio.gather，流式事件统一经 ctx.emit 输出。"""
from __future__ import annotations

import asyncio
from typing import Any, AsyncGenerator, Dict, List, TYPE_CHECKING

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
    if plan.use_agent_subgraph:
        ids.append("agent_goal_gate")
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
            ids.append(f"critic_legacy_uni_{i}")
            ids.append(f"critic_legacy_struct_{i}")
            ids.append(f"critic_merge_legacy_{i}")
        ids.extend([f"search_followup_{i}", f"repair_round_{i}", f"verify_round_{i}"])
    ids.append("finalize_output")
    return ids


def build_execution_dag(plan: "PlanDescription") -> DAG:
    """Planner 动态 DAG：layered 时 unified+5 facet 同波 gather；legacy 时 uni+struct gather；否则单节点 quality 轮。"""
    from runtime.nodes import dag_phases

    dag = DAG()
    dag.add(NodeSpec("parallel_search", deps=[], run=dag_phases.node_parallel_search))
    dag.add(NodeSpec("parallel_draft", deps=["parallel_search"], run=dag_phases.node_parallel_draft))
    prev = "parallel_draft"
    if plan.use_tool_gate:
        dag.add(NodeSpec("tool_capability_gate", deps=[prev], run=dag_phases.node_tool_capability_gate))
        prev = "tool_capability_gate"
    if plan.use_agent_subgraph:
        dag.add(NodeSpec("agent_goal_gate", deps=[prev], run=dag_phases.node_agent_goal_gate))
        prev = "agent_goal_gate"

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
            u = f"critic_legacy_uni_{i}"
            s = f"critic_legacy_struct_{i}"
            dag.add(NodeSpec(u, deps=[prelude], run=dag_phases.make_legacy_uni_runner(i)))
            dag.add(NodeSpec(s, deps=[prelude], run=dag_phases.make_legacy_struct_runner(i)))
            m2 = f"critic_merge_legacy_{i}"
            dag.add(NodeSpec(m2, deps=[u, s], run=dag_phases.make_critic_merge_legacy_runner(i)))
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
            use_agent_subgraph=False,
            use_tool_gate=False,
            parallel_drafts=True,
            hedge_draft_delay_ms=0,
            layered_critics=True,
        )
    )


async def stream_scheduled_dag(ctx: Any, dag: DAG, *, node_timeout_s: float = 180.0) -> AsyncGenerator[Dict[str, Any], None]:
    from runtime.streaming.chunk_router import augment_streaming_chunk

    q: asyncio.Queue = asyncio.Queue()

    async def emit(ev: Dict[str, Any]) -> None:
        if isinstance(ev, dict) and ev.get("event") == "chunk":
            ev = augment_streaming_chunk(ev)
        await q.put(ev)

    ctx.emit = emit
    sched = DAGScheduler(dag, node_timeout_s=node_timeout_s, max_retries=getattr(ctx, "dag_node_max_retries", 1))

    async def runner() -> None:
        try:

            async def err_emit(ev: Dict[str, Any]) -> None:
                await emit(ev)

            await sched.run(ctx, emit=err_emit)
        finally:
            await q.put(_SENTINEL)

    task = asyncio.create_task(runner())
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
