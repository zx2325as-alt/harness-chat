"""动作标记解析（Agent / Refine 审查层共用）。"""
from __future__ import annotations

import re

# <<ACTION: web_search("...")>>
RE_AGENT_WS = re.compile(r"<<ACTION:\s*web_search\s*\(\s*[\"']([^\"']+)[\"']\s*\)\s*>>", re.I)
# refine_answer：两段双引号内容，草稿内请勿含未转义 "
RE_AGENT_REFINE = re.compile(
    r"<<ACTION:\s*refine_answer\s*\(\s*\"([\s\S]*?)\"\s*,\s*\"([\s\S]*?)\"\s*\)\s*>>",
)
