from __future__ import annotations

import asyncio
from typing import Any, Awaitable, TypeVar

T = TypeVar("T")


async def race_tasks(primary: Awaitable[T], backup: Awaitable[T], *, delay_s: float = 0.8) -> T:
    """Hedged：backup 延迟启动，谁先成功用谁（简化版）。"""
    p_task = asyncio.create_task(primary)

    async def _delayed_backup():
        await asyncio.sleep(delay_s)
        return await backup

    b_task = asyncio.create_task(_delayed_backup())
    done, pending = await asyncio.wait({p_task, b_task}, return_when=asyncio.FIRST_COMPLETED)
    for t in pending:
        t.cancel()
    for t in done:
        try:
            return await t
        except Exception:
            continue
    raise RuntimeError("race_tasks: no successful task")
