"""预判 JSON：围栏剥离与平衡括号提取（供 harness / 测试复用）。"""
from __future__ import annotations

import re
from typing import Optional


def strip_markdown_json_fence(content: str) -> str:
    raw = (content or "").strip()
    m = re.search(r"```(?:json)?\s*(.*?)\s*```", raw, re.DOTALL | re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return raw


def extract_balanced_json_object(text: str) -> Optional[str]:
    """从混杂文本中提取首个花括号平衡的 JSON 对象子串。"""
    s = text or ""
    start = s.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(s)):
        ch = s[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return s[start : i + 1]
    return None
