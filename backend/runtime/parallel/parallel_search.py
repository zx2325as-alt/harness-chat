from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Tuple


async def parallel_web_search(
    harness: Any,
    queries: List[str],
    options: Dict[str, Any],
    *,
    overrides: Dict[str, Any] | None = None,
) -> List[Tuple[str, Dict[str, Any]]]:
    """并行检索：每项独立调用 harness.perform_web_search。"""

    qclean = [str(q).strip() for q in queries if str(q).strip()]
    if not qclean:
        return []

    sem = asyncio.Semaphore(min(4, max(1, len(qclean))))

    async def _one(q: str) -> Tuple[str, Dict[str, Any]]:
        async with sem:
            merged = {**(options or {}), **(overrides or {})}
            sr = await harness.perform_web_search(q, merged)
            return q, sr if isinstance(sr, dict) else {"error": "bad_search_result"}

    return list(await asyncio.gather(*[_one(q) for q in qclean]))
