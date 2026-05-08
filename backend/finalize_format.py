"""Refine Finalize：零模型、确定性排版（文档第五章 Finalize 硬性 ✓）。"""
from __future__ import annotations

import re
from typing import List


def format_finalize_markdown(text: str) -> str:
    """
    仅做安全、可预测的文本整理：换行规范化、行尾空白、ATX 标题空格、多余空行压缩。
    不调用 LLM，不引入新事实。
    """
    s = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    out: List[str] = []
    in_fence = False
    for raw in s.split("\n"):
        line = raw.rstrip()
        if line.strip().startswith("```"):
            in_fence = not in_fence
            out.append(line)
            continue
        if not in_fence and line.startswith("#"):
            line = re.sub(r"^(#{1,6})([^#\s])", r"\1 \2", line)
        out.append(line)
    blob = "\n".join(out)
    blob = re.sub(r"\n{4,}", "\n\n\n", blob)
    return blob.strip()
