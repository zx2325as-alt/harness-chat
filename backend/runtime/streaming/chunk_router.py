"""chunk 事件通道路由（draft / final / preliminary）+ Streaming Aggregator 元数据。"""
from __future__ import annotations

from typing import Any, Dict


def route_chunk_channel(event: Dict[str, Any]) -> str:
    if event.get("event") != "chunk":
        return "default"
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    return str(data.get("channel") or "default")


def augment_streaming_chunk(ev: Dict[str, Any]) -> Dict[str, Any]:
    """为 Progressive Streaming Aggregator 打上统一路由标签（不改变正文内容）。"""
    if ev.get("event") != "chunk":
        return ev
    data = ev.get("data")
    if not isinstance(data, dict):
        return ev
    ch = str(data.get("channel") or "default")
    merged = {
        **data,
        "streaming_aggregator": True,
        "routed_channel": ch,
    }
    return {**ev, "data": merged}

