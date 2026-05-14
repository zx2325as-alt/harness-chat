"""Repair Runtime Node：issue-targeted repair（RepairPlan 见 repair_engine）。"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, Tuple

from refine_shared import _pg
from runtime.dag_common import user_status
from runtime.kernel.runtime_context import DAGRuntimeContext
from runtime.quality.repair_engine import critic_issue_total, targeted_repair
from runtime_metrics import emit_product_metric


async def execute_round(
    ctx: DAGRuntimeContext,
    draft: str,
    merged: Dict[str, Any],
    ev_text: str,
    round_idx: int,
) -> Tuple[Any, str, bool]:
    h = ctx.harness
    opt = ctx.options
    st = ctx.st
    hcfg = ctx.hcfg
    trace_id = ctx.trace_id

    await ctx.emit(user_status("定点修复草案…", phase="repair"))
    await ctx.emit(
        {
            "event": "step",
            "step": {
                "name": "dag_repair",
                "status": "running",
                "meta": _pg({"round": round_idx}, "repair", "DAG：Repair Node"),
            },
        }
    )
    r_rep, repaired, guard_reverted = await targeted_repair(
        h,
        ctx.prompt,
        draft,
        merged,
        ev_text,
        opt,
        hcfg,
        ctx.repair_pool,
    )
    issue_n = critic_issue_total(merged)
    needs_q = [str(x).strip() for x in (merged.get("needs_search") or []) if str(x).strip()]
    skipped = issue_n == 0 and not needs_q
    await ctx.emit(
        {
            "event": "step",
            "step": {
                "name": "dag_repair",
                "status": "skipped" if skipped else "ok",
                "provider": getattr(r_rep, "provider", None) if r_rep else None,
                "model": getattr(r_rep, "model", None) if r_rep else None,
                "latency_ms": int(getattr(r_rep, "latency_ms", 0) or 0) if r_rep else 0,
                "meta": _pg({"guard_reverted": guard_reverted, "skipped": skipped}, "repair", "修复阶段结束。"),
                "error": getattr(r_rep, "error", None) if r_rep and not getattr(r_rep, "success", True) else None,
            },
        }
    )
    if not skipped and not guard_reverted and r_rep and getattr(r_rep, "success", False):
        emit_product_metric(hcfg, "repair_success_rate", trace_id=trace_id, round=round_idx)
        if st:
            st.runtime_memory.append({"phase": "repair", "round": round_idx, "ok": True})
        snap = repaired.strip()
        if snap:
            excerpt = snap[:320] + ("…" if len(snap) > 320 else "")
            await ctx.emit({"event": "chunk", "data": {"content": excerpt, "channel": "progressive_revision"}})
            await ctx.emit(
                {"event": "chunk", "data": {"content": excerpt, "channel": "stream_repair", "round": round_idx}}
            )
            for hk in list(opt.get("_stream_repair_hooks") or []):
                try:
                    out = hk(snap[:4000])
                    if asyncio.iscoroutine(out):
                        await out
                except Exception:
                    pass
    return r_rep, repaired, guard_reverted
