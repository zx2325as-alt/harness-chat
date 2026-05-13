"""投机执行：fire-and-forget 协程任务（与 harness 投机检索对齐）。"""
from __future__ import annotations

import asyncio
from typing import Awaitable, Optional


def fire_and_forget(coro: Awaitable[Optional[object]], *, label: str = "") -> asyncio.Task:
    del label

    async def _wrap():
        try:
            await coro
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    return asyncio.create_task(_wrap())
