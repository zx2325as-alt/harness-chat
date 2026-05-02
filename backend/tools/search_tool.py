"""联网搜索工具封装（统一入口，底层仍为 SearchService）。"""
from __future__ import annotations

from typing import Any, Dict, TYPE_CHECKING

if TYPE_CHECKING:
    from harness import DualTrackHarness


async def web_search(harness: "DualTrackHarness", query: str) -> Dict[str, Any]:
    """执行联网检索，返回与 SearchService.search 一致的结构。"""
    q = (query or "").strip()
    if not q:
        return {"context": "", "sources": [], "error": "empty_query"}
    return await harness.perform_web_search(q)
