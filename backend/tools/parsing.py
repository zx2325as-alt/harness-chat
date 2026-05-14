"""运行时检索动作解析：仅保留 review 中的 web_search JSON 提取。"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple

from json_utils import extract_balanced_json_object


def _iter_json_object_starts(text: str, max_starts: int = 32) -> List[int]:
    return [m.start() for m in re.finditer(r"\{", text or "")][:max_starts]


def extract_first_web_search_json(text: str) -> Optional[Dict[str, Any]]:
    """从混排正文中提取首个 web_search JSON 对象。"""
    s = text or ""
    for start in _iter_json_object_starts(s):
        blob = extract_balanced_json_object(s[start:])
        if not blob:
            continue
        try:
            obj = json.loads(blob)
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
        if str(obj.get("action") or "").strip().lower() != "web_search":
            continue
        q = str(obj.get("query") or "").strip()
        if q:
            return obj
    return None


def next_review_search_action(review_body: str) -> Tuple[Optional[str], str]:
    """仅 JSON 协议。返回 (query, 'json'|'')。"""
    js = extract_first_web_search_json(review_body or "")
    if js:
        return str(js.get("query") or "").strip(), "json"
    return None, ""
