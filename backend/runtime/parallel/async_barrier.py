"""依赖屏障占位：与 DAG 波次 asyncio.gather 组合；完整计数屏障可按节点维度扩展。"""
from __future__ import annotations

import asyncio


class AsyncBarrier:
    def __init__(self, parties: int) -> None:
        self.parties = max(1, parties)
        self._event = asyncio.Event()
        self._count = 0

    async def wait(self) -> None:
        await self._event.wait()

    def signal(self) -> None:
        self._count += 1
        if self._count >= self.parties:
            self._event.set()
