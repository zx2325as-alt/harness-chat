"""验证层：claim extraction → evidence mapping → contradiction → unsupported 启发式 → verify_answer(JSON)。"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple

from unified_critic import verify_answer


def extract_claims_from_draft(draft: str, *, max_claims: int = 28) -> List[str]:
    """粗粒度断言切分：句界分段。"""
    text = str(draft or "").strip()
    if not text:
        return []
    parts = re.split(r"(?<=[。！？.!?])\s+", text)
    out = [p.strip() for p in parts if len(p.strip()) > 14]
    if len(out) < 4:
        out = [text[i : i + 160].strip() for i in range(0, min(len(text), 1200), 140) if text[i : i + 160].strip()]
    return out[:max_claims]


def map_claims_to_evidence_snippets(claims: List[str], evidence_text: str, *, max_pairs: int = 16) -> List[Dict[str, Any]]:
    """证据映射：对每个 claim 找证据块内最长公共子串启发式（字符 bigram 重叠）。"""
    ev = str(evidence_text or "").lower()
    pairs: List[Dict[str, Any]] = []
    for c in claims[:24]:
        cl = c.lower()[:400]
        best_j, best_ov = -1, 0
        window = 180
        for j in range(0, min(len(ev), 6000), 40):
            seg = ev[j : j + window]
            ov = len(set(cl[i : i + 2] for i in range(0, max(0, len(cl) - 1), 2)) & set(seg[i : i + 2] for i in range(0, max(0, len(seg) - 1), 2)))
            if ov > best_ov:
                best_ov, best_j = ov, j
        snippet = evidence_text[best_j : best_j + window] if best_j >= 0 and evidence_text else ""
        pairs.append({"claim": c[:240], "overlap_score": best_ov, "evidence_snippet": snippet[:320]})
        if len(pairs) >= max_pairs:
            break
    return pairs


def contradiction_check_heuristic(
    claims: List[str],
    evidence_text: str,
    mapping: List[Dict[str, Any]],
) -> Tuple[bool, List[str]]:
    """矛盾检测启发式：高重叠 claim 对与证据否定词共现。"""
    issues: List[bool] = []
    neg = ("不成立", "错误", "否认", "contradict", "false", "incorrect", "no evidence")
    ev_low = str(evidence_text or "").lower()
    for m in mapping:
        sn = str(m.get("evidence_snippet") or "").lower()
        ov = int(m.get("overlap_score") or 0)
        bad = ov > 8 and any(x in sn for x in neg)
        issues.append(bad)
    cross = False
    if len(claims) >= 2:
        a, b = claims[0].lower()[:80], claims[1].lower()[:80]
        if ("是" in a and "否" in b) or ("支持" in a and "反对" in b):
            cross = True
    hits = [i for i, x in enumerate(issues) if x]
    msgs = [f"mapping_contradiction_hint:{i}" for i in hits[:4]]
    if cross:
        msgs.append("claim_pair_polarity_contrast")
    return bool(hits or cross), msgs


def unsupported_claims_heuristic(mapping: List[Dict[str, Any]]) -> List[str]:
    """unsupported：与证据重叠极低的断言。"""
    out: List[str] = []
    for m in mapping:
        if int(m.get("overlap_score") or 0) < 2:
            c = str(m.get("claim") or "").strip()
            if c:
                out.append(c[:200])
    return out[:12]


async def run_verify_with_evidence_mapping(
    harness: Any,
    prompt: str,
    draft: str,
    analysis: Dict[str, Any],
    options: Dict[str, Any],
    hcfg: Dict[str, Any],
    *,
    search_context: str = "",
) -> Dict[str, Any]:
    claims = extract_claims_from_draft(draft)
    mapping = map_claims_to_evidence_snippets(claims, search_context)
    contra, contra_msgs = contradiction_check_heuristic(claims, search_context, mapping)
    unsupported = unsupported_claims_heuristic(mapping)

    uc = await verify_answer(
        harness,
        prompt,
        draft,
        analysis,
        options,
        hcfg,
        search_context=search_context,
    )
    if isinstance(uc, dict):
        uc = dict(uc)
        uc["claims_extracted"] = claims[:24]
        uc["claims_count"] = len(claims)
        uc["claim_evidence_mapping"] = mapping[:16]
        uc["contradiction_heuristic"] = {"hit": contra, "signals": contra_msgs}
        uc["unsupported_claims_heuristic"] = unsupported
    return uc
