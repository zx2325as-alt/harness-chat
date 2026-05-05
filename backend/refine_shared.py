"""Refine 流水线共享类型与纯函数：供 harness 与 tools.refine_pipeline 共用，避免 tools ← harness 循环依赖。"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, Optional


_RE_FINAL_BLOCK = re.compile(r"(?is)^\s*<<<FINAL_ANSWER>>>\s*\n([\s\S]*?)\n\s*<<<END_FINAL_ANSWER>>>\s*$")


def _pg(meta: Optional[Dict[str, Any]], phase_group: str, event_summary: str) -> Dict[str, Any]:
    m = dict(meta or {})
    m["phase_group"] = phase_group
    m["event_summary"] = event_summary
    return m


def _int_budget(options: Optional[Dict[str, Any]], key: str, default: int, *, minimum: int, maximum: int) -> int:
    try:
        value = int((options or {}).get(key, default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _clean_review_body(review_body: str) -> str:
    text = str(review_body or "").strip()
    if not text:
        return ""

    mfb = _RE_FINAL_BLOCK.match(text)
    if mfb:
        body = str(mfb.group(1) or "").strip()
        if body:
            return body

    m = re.search(r"(?is)(?:修正版答案|最终答案|答案正文|修正后答案)\s*[:：]\s*", text)
    if m:
        rest = text[m.end() :]
        parts = re.split(r"(?is)(?:\n\s*)(?:仍不确定处|不确定点|问题清单)\s*[:：]", rest, maxsplit=1)
        body = parts[0].strip()
        if body:
            return body

    text = re.sub(r"(?is)^\s*初稿问题清单\s*[:：].*?(?=\n\s*(?:修正版答案|最终答案)|\Z)", "", text)
    tail_pat = re.compile(r"(?is)(\n\s*(?:仍不确定处|不确定点|问题清单)\s*[:：].*)$")
    m2 = tail_pat.search(text)
    if m2 is not None and m2.start() >= max(24, int(len(text) * 0.22)):
        text = text[: m2.start()].strip()

    return text.strip()


@dataclass
class Step:
    name: str
    status: str
    provider: Optional[str] = None
    model: Optional[str] = None
    latency_ms: Optional[int] = None
    input_preview: Optional[str] = None
    output: Optional[str] = None
    error: Optional[str] = None
    meta: Optional[Dict[str, Any]] = None

    def _step_id(self) -> str:
        meta = self.meta or {}
        if meta.get("step_id"):
            return str(meta["step_id"])
        pg = str(meta.get("phase_group") or meta.get("pipeline_phase") or "")
        if self.name == "agent_iteration" and meta.get("i") is not None:
            return f"{self.name}:{meta.get('i')}"
        if self.name == "review_web_search" and meta.get("review_round") is not None:
            return f"{self.name}:{pg}:{meta.get('review_round')}"
        if self.name == "agent_web_search" and meta.get("query"):
            return f"{self.name}:{meta.get('query')}"
        return f"{self.name}:{pg}" if pg else self.name

    def to_dict(self) -> Dict[str, Any]:
        meta = dict(self.meta or {})
        sid = self._step_id()
        meta.setdefault("step_id", sid)
        meta.setdefault("parent_id", meta.get("phase_group") or meta.get("pipeline_phase") or "")
        return {
            "step_id": sid,
            "name": self.name,
            "status": self.status,
            "provider": self.provider,
            "model": self.model,
            "latency_ms": self.latency_ms,
            "input_preview": self.input_preview,
            "output": self.output,
            "error": self.error,
            "meta": meta,
        }
