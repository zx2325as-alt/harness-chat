"""动作标记解析（Agent / Refine 审查层共用）。"""
from __future__ import annotations

import re
import json
from typing import Any, Dict, Optional

# <<ACTION: web_search("...")>>
RE_AGENT_WS = re.compile(r"<<ACTION:\s*web_search\s*\(\s*[\"']([^\"']+)[\"']\s*\)\s*>>", re.I)
# refine_answer：两段双引号内容，草稿内请勿含未转义 "
RE_AGENT_REFINE = re.compile(
    r"<<ACTION:\s*refine_answer\s*\(\s*\"([\s\S]*?)\"\s*,\s*\"([\s\S]*?)\"\s*\)\s*>>",
)


def _json_action_strict(text: str) -> Optional[Dict[str, Any]]:
    """
    仅当整段（或整段 fenced）为合法 JSON 对象且含 action 字段时才解析。
    避免从「解释文字 + JSON」中贪婪截取子串导致误触发工具分支。
    """
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


def parse_agent_action(text: str) -> Dict[str, Any]:
    """优先严格 JSON action；否则回退到 <<ACTION: ...>> 标记。"""
    obj = _json_action_strict(text)
    if obj:
        action = str(obj.get("action") or "").strip()
        if action == "web_search":
            return {"action": action, "query": str(obj.get("query") or "").strip()}
        if action == "refine_answer":
            return {
                "action": action,
                "question": str(obj.get("question") or obj.get("orig_q") or "").strip(),
                "draft": str(obj.get("draft") or obj.get("answer") or "").strip(),
            }

    rm = RE_AGENT_REFINE.search(text or "")
    if rm:
        return {"action": "refine_answer", "question": (rm.group(1) or "").strip(), "draft": (rm.group(2) or "").strip()}
    wm = RE_AGENT_WS.search(text or "")
    if wm:
        return {"action": "web_search", "query": (wm.group(1) or "").strip()}
    return {"action": ""}
