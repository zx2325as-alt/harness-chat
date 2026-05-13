"""Progressive Runtime Streaming：元数据路由与渐进式通道占位（与 chunk_router 协同）。"""
from __future__ import annotations

from typing import Any, Dict, Optional

from runtime.streaming.chunk_router import route_chunk_channel


class ProgressiveStreamRouter:
    """聚合渐进式通道标签；具体 token 流仍由 harness chunk 管道承载。"""

    def __init__(self, options: Dict[str, Any]) -> None:
        self.options = options

    async def emit_preliminary_note(self, intent_snapshot: Dict[str, Any]) -> None:
        self.options.setdefault("_progressive_stream", {})["intent_snapshot"] = intent_snapshot

    def route(self, event: Dict[str, Any]) -> str:
        return route_chunk_channel(event)
