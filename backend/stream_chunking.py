"""流式输出：可选按中英文标点优先切分 chunk，减少「半截词」观感。"""
from __future__ import annotations

from typing import List, Tuple

_BREAK_CHARS = frozenset("，。！？；、,.!?;\n\r")

__all__ = ["iter_chunk_spans"]


def iter_chunk_spans(text: str, step: int, *, smart: bool) -> List[Tuple[int, int]]:
    """返回每个 slice 的 [start, end) 半开区间。"""
    step = max(1, int(step))
    n = len(text or "")
    if n == 0:
        return []
    if not smart or step <= 8:
        return [(i, min(i + step, n)) for i in range(0, n, step)]

    out: List[Tuple[int, int]] = []
    i = 0
    while i < n:
        end = min(i + step, n)
        if end >= n:
            out.append((i, n))
            break
        search_from = max(i, end - max(8, step // 3))
        cut_pos = -1
        for pos in range(end - 1, search_from - 1, -1):
            if text[pos] in _BREAK_CHARS:
                cut_pos = pos + 1
                break
        if cut_pos > i:
            out.append((i, cut_pos))
            i = cut_pos
        else:
            out.append((i, end))
            i = end
    return out
