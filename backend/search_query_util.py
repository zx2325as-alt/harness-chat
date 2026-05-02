"""检索 query 校验与规范化（避免 Tavily 400、空 query、过长等）。"""
from __future__ import annotations

import re
from typing import Optional, Tuple

# Tavily 等常见上限；留出余量
MAX_QUERY_CHARS = 380
MIN_QUERY_CHARS = 2


def validate_search_query(raw: str, *, max_chars: int = MAX_QUERY_CHARS) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    返回 (规范化后的 query, failure_code, 人类可读原因)。
    成功时 failure_code 与 原因为 None。
    """
    q = (raw or "").strip()
    if not q:
        return None, "EMPTY_QUERY", "检索词为空"
    q = re.sub(r"\s+", " ", q)
    if len(q) < MIN_QUERY_CHARS:
        return None, "QUERY_TOO_SHORT", f"检索词过短（需至少 {MIN_QUERY_CHARS} 个有效字符）"
    # 仅标点 / 占位
    alnum = re.sub(r"[\W_]+", "", q, flags=re.UNICODE)
    if len(alnum) < MIN_QUERY_CHARS:
        return None, "QUERY_NON_ALPHANUMERIC", "检索词仅含标点或无效占位"
    if re.fullmatch(r"[.…\-_=+]+", q):
        return None, "QUERY_PLACEHOLDER", "检索词为无意义占位"
    # 未闭合引号（简单启发）
    if q.count('"') % 2 == 1 or q.count("'") % 2 == 1:
        return None, "QUERY_UNBALANCED_QUOTES", "检索词引号未闭合，已拒绝原样检索"
    bad_samples = ("示例查询", "example query", "your query", "查询词", "placeholder")
    low = q.lower()
    if any(s in q for s in bad_samples) or "lorem" in low:
        return None, "QUERY_TEMPLATE", "疑似模板/示例占位检索词"

    if len(q) > max_chars:
        q = q[:max_chars].rsplit(" ", 1)[0] if " " in q[:max_chars] else q[:max_chars]
        q = q.strip() or (raw or "").strip()[:max_chars]
    return q, None, None


def soft_degrade_note(failure_code: Optional[str], err: Optional[str]) -> str:
    base = "【系统说明】未能完成实时联网检索，以下为不依赖本次检索结果的回答。"
    if failure_code:
        base += f"（原因码：{failure_code}）"
    if err:
        base += f" 详情：{str(err)[:200]}"
    return base
