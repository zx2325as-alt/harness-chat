"""验证层：claim extraction → evidence mapping（dense/ngram） → contradiction → unsupported → verify_answer(JSON)。"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple  # noqa: F401

from unified_critic import verify_answer


def extract_claims_from_draft(draft: str, *, max_claims: int = 28) -> List[str]:
    """粗粒度断言切分：句界分段。"""
    text = str(draft or "").strip()
    if not text:
        return []
    parts = re.split(r"(?<=[。！？.!?])\s+", text)
    out = [p.strip() for p in parts if len(p.strip()) > 14]
    if len(out) < 4:
        out = [text[i : i + 160].strip() for i in range(0, min(len(text), 1200), 140)
               if text[i : i + 160].strip()]
    return out[:max_claims]


def _build_segments(evidence_text: str, window: int = 300, step: int = 80) -> List[Tuple[int, str]]:
    """将 evidence_text 切为滑动窗口 segments，供映射打分。"""
    ev = str(evidence_text or "")
    segs: List[Tuple[int, str]] = []
    for j in range(0, min(len(ev), 10000), step):
        seg = ev[j : j + window]
        if seg.strip():
            segs.append((j, seg))
    return segs


async def map_claims_to_evidence_snippets(
    claims: List[str],
    evidence_text: str,
    *,
    max_pairs: int = 16,
    model_cfg: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """证据映射：优先 dense embedding（batch_semantic_similarity_online），fallback ngram_overlap_ratio。"""
    from semantic_utils import batch_semantic_similarity_online, ngram_overlap_ratio

    ev = str(evidence_text or "")
    if not ev.strip() or not claims:
        return []

    window = 300
    step = 80
    segments = _build_segments(ev, window=window, step=step)
    if not segments:
        return []

    seg_texts = [s for _, s in segments]
    pairs: List[Dict[str, Any]] = []

    # 尝试 dense embedding（需要 API 调用；失败则 fallback ngram）
    use_dense = True
    dense_scores_per_claim: List[List[float]] = []
    try:
        # batch: [claim1, claim2, ...] × segments — 每个 claim 单独一次调用
        for c in claims[:max_pairs]:
            scores = await batch_semantic_similarity_online(
                c[:400], seg_texts, model_cfg=model_cfg, timeout_s=8.0
            )
            dense_scores_per_claim.append(scores)
    except Exception:
        use_dense = False
        dense_scores_per_claim = []

    for idx, c in enumerate(claims[:max_pairs]):
        cl = c[:400]
        if use_dense and idx < len(dense_scores_per_claim):
            scores = dense_scores_per_claim[idx]
            if scores:
                best_seg_idx = max(range(len(scores)), key=lambda i: scores[i])
                best_score = float(scores[best_seg_idx])
                best_j = segments[best_seg_idx][0]
            else:
                best_j, best_score = -1, 0.0
        else:
            # fallback: ngram_overlap_ratio
            best_j, best_score = -1, 0.0
            for j, seg in segments:
                score = ngram_overlap_ratio(cl.lower(), seg.lower())
                if score > best_score:
                    best_score, best_j = score, j

        snippet = ev[best_j : best_j + window] if best_j >= 0 else ""
        pairs.append({
            "claim": c[:240],
            "overlap_score": round(best_score, 4),
            "evidence_snippet": snippet[:320],
            "method": "dense" if (use_dense and idx < len(dense_scores_per_claim)) else "ngram",
        })

    return pairs


def contradiction_check_heuristic(
    claims: List[str],
    evidence_text: str,
    mapping: List[Dict[str, Any]],
) -> Tuple[bool, List[str]]:
    """矛盾检测：高相似度 snippet 若含否定词则标记矛盾；dense 阈值与 ngram 阈值分开。"""
    from semantic_utils import ngram_overlap_ratio
    issues: List[bool] = []
    neg = ("不成立", "错误", "否认", "contradict", "false", "incorrect", "no evidence",
           "不正确", "不支持", "无证据", "refuted")
    for m in mapping:
        sn = str(m.get("evidence_snippet") or "").lower()
        score = float(m.get("overlap_score") or 0.0)
        method = str(m.get("method") or "ngram")
        # dense cosine ≥ 0.72 为高相关；ngram ≥ 0.15
        threshold = 0.72 if method == "dense" else 0.15
        bad = score > threshold and any(x in sn for x in neg)
        issues.append(bad)
    cross = False
    if len(claims) >= 2:
        a, b = claims[0].lower()[:120], claims[1].lower()[:120]
        polarity_a = ("是" in a or "支持" in a or "正确" in a)
        polarity_b = ("否" in b or "反对" in b or "错误" in b)
        if polarity_a and polarity_b and ngram_overlap_ratio(a, b) > 0.2:
            cross = True
    hits = [i for i, x in enumerate(issues) if x]
    msgs = [f"mapping_contradiction_hint:{i}" for i in hits[:4]]
    if cross:
        msgs.append("claim_pair_polarity_contrast")
    return bool(hits or cross), msgs


def unsupported_claims_heuristic(mapping: List[Dict[str, Any]]) -> List[str]:
    """unsupported：dense cosine < 0.35 或 ngram < 0.05 的断言。"""
    out: List[str] = []
    for m in mapping:
        score = float(m.get("overlap_score") or 0.0)
        method = str(m.get("method") or "ngram")
        threshold = 0.35 if method == "dense" else 0.05
        if score < threshold:
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

    # 获取 embedding model config（若有）
    embed_cfg: Optional[Dict[str, Any]] = None
    try:
        models_cfg = hcfg.get("models") or {}
        embed_cfg = models_cfg.get("text-embedding-3-large") or None
    except Exception:
        pass

    mapping = await map_claims_to_evidence_snippets(
        claims, search_context, model_cfg=embed_cfg
    )
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
        # 记录本次使用的映射方式（dense/ngram）
        methods = list({m.get("method", "ngram") for m in mapping})
        uc["mapping_method"] = methods[0] if len(methods) == 1 else "mixed"
    return uc
