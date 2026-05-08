"""SSE chunk 通道：区分草稿/终稿，供服务端历史与前端展示过滤。"""
from __future__ import annotations

from typing import Any, Dict, Optional


def chunk_writes_history(data: Dict[str, Any]) -> bool:
    """仅 channel=final（或未标注 channel，兼容旧客户端）写入 Redis；draft/tool/internal 不写历史。"""
    ch = str((data or {}).get("channel") or "").strip().lower()
    return ch in ("", "final")


def attach_chunk_channel(
    event: Dict[str, Any],
    channel: str,
    options: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if str(event.get("event") or "") != "chunk":
        return event
    data = dict(event.get("data") or {})
    data["channel"] = channel
    if options is not None:
        try:
            seq = int(options.get("_chunk_seq", -1)) + 1
        except (TypeError, ValueError):
            seq = 0
        options["_chunk_seq"] = seq
        data["sequence"] = seq
    return {**event, "data": data}
