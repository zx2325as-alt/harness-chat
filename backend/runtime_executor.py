"""统一 Runtime：SSE 与同步 API 共用同一事件流（run_stream），同步侧仅做聚合。"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from chunk_channels import chunk_writes_history
from model_adapters import AskResult
from runtime_state import runtime_phase
from sse_public import normalize_step_for_client


async def execute_runtime(
    harness: Any,
    prompt: str,
    mode: str,
    options: Optional[Dict[str, Any]],
    messages: Optional[List[Dict[str, Any]]],
):
    """统一 Runtime 迭代入口：委托 ``RuntimeHarness.run_stream``（HTTP SSE 与同步 REST 共用同一事件序列）。"""
    async for ev in harness.run_stream(prompt, mode=mode, options=options or {}, messages=messages):
        yield ev


async def collect_sync_response_from_stream(
    harness: Any,
    prompt: str,
    mode: str,
    options: Optional[Dict[str, Any]],
    messages: Optional[List[Dict[str, Any]]],
) -> Dict[str, Any]:
    """将流式事件聚合为同步 REST 响应（与历史 ``run`` 返回形状兼容）。"""
    opts = dict(options or {})
    steps_out: List[Dict[str, Any]] = []
    buf: List[str] = []
    trace_out = str(opts.get("trace_id") or "")
    stream_failed = False
    error_msg: Optional[str] = None
    last_model = "unified_stream"
    last_provider = "runtime"
    total_latency = 0

    async for ev in execute_runtime(harness, prompt, mode, opts, messages):
        et = str(ev.get("event") or "")
        if et == "trace" and ev.get("trace_id"):
            trace_out = str(ev.get("trace_id"))
        elif et == "step" and isinstance(ev.get("step"), dict):
            steps_out.append(normalize_step_for_client(dict(ev["step"])))
        elif et == "content_reset":
            buf.clear()
        elif et == "chunk":
            data = ev.get("data") or {}
            if chunk_writes_history(data):
                buf.append(str(data.get("content") or ""))
        elif et == "error":
            stream_failed = True
            error_msg = str(ev.get("error") or "stream_error")
        elif et == "model_end":
            try:
                total_latency += int(ev.get("latency_ms") or 0)
            except (TypeError, ValueError):
                pass
            if ev.get("model"):
                last_model = str(ev.get("model"))
            if ev.get("provider"):
                last_provider = str(ev.get("provider"))

    text = "".join(buf)
    phase = runtime_phase(opts)
    sync_meta: Dict[str, Any] = {
        "protocol_version": 1,
        "api_schema": "harness-sync-v1",
        "unified_stream_runtime": True,
        "runtime": "adaptive_dag_v3",
        "phase": phase,
    }
    if opts.get("_runtime_repair_triggered"):
        sync_meta["sync_used_repair_loop"] = True
    if opts.get("_goal_capability_gate"):
        sync_meta["goal_capability_gate"] = True
    if any(isinstance(s, dict) and s.get("name") == "agent_iteration" for s in steps_out):
        sync_meta["sync_agent"] = True

    if stream_failed:
        final = AskResult(
            False,
            text,
            last_provider,
            last_model,
            total_latency,
            error=error_msg or "stream_failed",
        ).to_dict()
    else:
        final = AskResult(True, text, last_provider, last_model, total_latency).to_dict()

    return {
        "trace_id": trace_out or opts.get("trace_id"),
        "final": final,
        "steps": steps_out,
        "meta": sync_meta,
    }
