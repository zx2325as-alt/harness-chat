"""
统一工具层入口：对 DualTrackHarness 暴露语义化方法，避免业务散落多处直接调底层。
（不包含代码执行器 / 沙箱。）
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, TYPE_CHECKING

from tools.refine_pipeline import compile_agent_fallback_draft, stream_refine_from_draft as stream_refine_pipeline
from tools.search_tool import web_search

if TYPE_CHECKING:
    from harness import DualTrackHarness


class HarnessTools:
    """挂载在 Harness 上的工具门面，供 Agent / 路由代码使用。"""

    __slots__ = ("_h",)

    def __init__(self, harness: "DualTrackHarness"):
        self._h = harness

    async def web_search(self, query: str, options: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return await web_search(self._h, query, options)

    async def stream_refine_from_draft(
        self,
        question: str,
        draft_text: str,
        options: Dict[str, Any],
        messages: Optional[List[Dict[str, Any]]],
        trace_id: str,
        hcfg: Dict[str, Any],
        analysis: Dict[str, Any],
        *,
        meta_extra: Optional[Dict[str, Any]] = None,
    ):
        async for ev in stream_refine_pipeline(
            self._h,
            question,
            draft_text,
            options,
            messages,
            trace_id,
            hcfg,
            analysis,
            meta_extra=meta_extra,
        ):
            yield ev

    @staticmethod
    def compile_agent_fallback_draft(conv: List[Dict[str, Any]], user_prompt: str, max_chars: int = 12000) -> str:
        return compile_agent_fallback_draft(conv, user_prompt, max_chars=max_chars)
