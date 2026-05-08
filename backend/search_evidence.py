"""结构化搜索证据与充足性评估（文档第七章）。"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List

from json_utils import extract_balanced_json_object, strip_markdown_json_fence


@dataclass
class SearchEvidence:
    source: str
    content: str
    freshness_score: float = 0.5
    trust_score: float = 0.5
    relevance_score: float = 0.5
    supports_claims: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def search_result_to_evidence(sr: Dict[str, Any]) -> List[SearchEvidence]:
    """将 SearchService 返回的 sources/context 转为证据列表。"""
    out: List[SearchEvidence] = []
    ctx = str(sr.get("context") or "").strip()
    if ctx:
        out.append(
            SearchEvidence(
                source="merged_context",
                content=ctx[:12000],
                freshness_score=0.5,
                trust_score=0.5,
                relevance_score=0.7,
                supports_claims=[],
            )
        )
    for s in sr.get("sources") or []:
        if not isinstance(s, dict):
            continue
        url = str(s.get("url") or s.get("title") or "source")
        snippet = str(s.get("snippet") or s.get("content") or "")[:4000]
        if not snippet:
            continue
        auth = float(s.get("authority_score") or s.get("score") or 0.5)
        out.append(
            SearchEvidence(
                source=url[:500],
                content=snippet,
                freshness_score=0.5,
                trust_score=max(0.0, min(1.0, auth)),
                relevance_score=float(s.get("relevance_score") or 0.6),
                supports_claims=[],
            )
        )
    return out


def evidence_bundle_text(evidence: List[SearchEvidence], max_chars: int = 8000) -> str:
    parts: List[str] = []
    used = 0
    for i, e in enumerate(evidence[:20]):
        line = f"[{i + 1}] {e.source}\n{e.content}\n"
        if used + len(line) > max_chars:
            break
        parts.append(line)
        used += len(line)
    return "\n".join(parts)


async def evaluate_search_sufficiency(
    harness: Any,
    user_question: str,
    evidence_text: str,
    options: Dict[str, Any],
    hcfg: Dict[str, Any],
) -> Dict[str, Any]:
    """判断当前检索是否足以回答用户问题。"""
    defaults = {
        "sufficient": True,
        "missing_dimensions": [],
        "contradictions": [],
        "confidence": 0.5,
        "parse_ok": False,
    }
    orch = hcfg.get("runtime_orchestrator") if isinstance(hcfg.get("runtime_orchestrator"), dict) else {}
    sr_cfg = orch.get("search_sufficiency") if isinstance(orch.get("search_sufficiency"), dict) else {}
    if not bool(sr_cfg.get("enabled", True)):
        return {**defaults, "parse_ok": True, "sufficient": True}

    routing = hcfg.get("routing") or {}
    key = str(sr_cfg.get("model_key") or "").strip()
    cands: List[str] = []
    if key:
        cands.append(key)
    dm = str(routing.get("default_model") or "").strip()
    if dm:
        cands.append(dm)
    reg = getattr(harness, "registry", None)
    is_reg = getattr(reg, "is_registered", lambda x: False)
    cands = [m for m in cands if is_reg(m)]
    if not cands:
        return defaults

    prompt = (
        "你是检索充足性评估器，只输出 JSON。\n"
        "字段：sufficient(bool), missing_dimensions(string[]), contradictions(string[]), confidence(0-1)。\n"
        "若证据不足以覆盖用户问题的关键维度，sufficient=false。\n\n"
        f"【用户问题】\n{(user_question or '')[:3000]}\n\n【检索证据】\n{(evidence_text or '')[:10000]}\n"
    )
    opts = {**options, "temperature": 0.05, "max_retries": 0}
    res, _ = await harness._ask_with_fallback(cands, prompt, opts, messages=None)
    if not res or not res.success:
        return defaults

    raw_txt = strip_markdown_json_fence(res.content or "")
    blob = extract_balanced_json_object(raw_txt) or raw_txt
    try:
        data = json.loads(blob) if isinstance(blob, str) else {}
    except json.JSONDecodeError:
        return defaults

    def sl(k: str) -> List[str]:
        v = data.get(k)
        if not isinstance(v, list):
            return []
        return [str(x).strip() for x in v[:12] if str(x).strip()]

    try:
        conf = float(data.get("confidence", 0.5))
    except (TypeError, ValueError):
        conf = 0.5

    return {
        "sufficient": bool(data.get("sufficient", True)),
        "missing_dimensions": sl("missing_dimensions"),
        "contradictions": sl("contradictions"),
        "confidence": max(0.0, min(1.0, conf)),
        "parse_ok": True,
    }
