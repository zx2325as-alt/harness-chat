"""结构化 JSON 工具协议（Agent / Refine 审查层）；不支持旧式括号工具语法。"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple

from json_utils import extract_balanced_json_object


def _json_action_strict(text: str) -> Optional[Dict[str, Any]]:
    """整段（或 fenced）为单一 JSON 对象且含 action。"""
    s = (text or "").strip()
    if not s:
        return None
    m = re.fullmatch(r"```(?:json)?\s*([\s\S]*?)\s*```", s, flags=re.I)
    if m:
        s = m.group(1).strip()
    if not (s.startswith("{") and s.endswith("}")):
        return None
    try:
        obj = json.loads(s)
    except Exception:
        return None
    return obj if isinstance(obj, dict) and "action" in obj else None


def _iter_json_object_starts(text: str, max_starts: int = 32) -> List[int]:
    return [m.start() for m in re.finditer(r"\{", text or "")][:max_starts]


def _parse_tool_dict(obj: Dict[str, Any]) -> Dict[str, Any]:
    action = str(obj.get("action") or "").strip().lower()
    if action == "web_search":
        q = str(obj.get("query") or "").strip()
        if q:
            return {"action": "web_search", "query": q}
    if action == "refine_answer":
        qn = str(obj.get("question") or obj.get("orig_q") or "").strip()
        dr = str(obj.get("draft") or obj.get("answer") or "").strip()
        if qn and dr:
            return {"action": "refine_answer", "question": qn, "draft": dr}
    return {"action": ""}


def parse_agent_action(text: str) -> Dict[str, Any]:
    """从正文中提取首个合法工具 JSON（可夹杂解释文字）。"""
    obj = _json_action_strict(text or "")
    if obj:
        out = _parse_tool_dict(obj)
        if out.get("action"):
            return out
    s = text or ""
    for start in _iter_json_object_starts(s):
        blob = extract_balanced_json_object(s[start:])
        if not blob:
            continue
        try:
            jo = json.loads(blob)
        except Exception:
            continue
        if not isinstance(jo, dict):
            continue
        out = _parse_tool_dict(jo)
        if out.get("action"):
            return out
    return {"action": ""}


def strip_text_after_first_tool_json(text: str) -> str:
    """截断到首个完整工具 JSON 结尾，避免把 JSON 混进对外正文流。"""
    s = text or ""
    for start in _iter_json_object_starts(s):
        blob = extract_balanced_json_object(s[start:])
        if not blob:
            continue
        try:
            jo = json.loads(blob)
        except Exception:
            continue
        if not isinstance(jo, dict):
            continue
        act = str(jo.get("action") or "").strip().lower()
        if act not in ("web_search", "refine_answer"):
            continue
        end = start + len(blob)
        return s[:end].rstrip()
    return s


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
