"""Multi-stage Retrieval Runtime：rewrite → expansion → dedup → rerank → 并行查询前整形。"""
from __future__ import annotations

import json
import re
from typing import Any, Callable, Dict, List, Optional

from search_evidence import SearchEvidence

_REWRITE_PROMPT = """\
你是搜索查询优化专家。将用户的原始查询改写为更适合搜索引擎的精确查询。
要求：
1. 消除歧义，补全缺失的关键词
2. 拆解复合问题为1-3个独立的精准查询
3. 每行一个查询，不要编号，不要任何解释

原始查询：{query}

直接输出改写后的查询（每行一个）："""


def rewrite_query(q: str) -> str:
    """同步直通（搜索前无法调用 LLM 时使用）。"""
    q = str(q or "").strip()
    return q[:500]


async def rewrite_query_llm(
    q: str,
    harness: Any,
    options: Dict[str, Any],
    hcfg: Dict[str, Any],
    *,
    models: Optional[List[str]] = None,
) -> List[str]:
    """LLM 驱动的查询改写：返回 1–3 条改写后的查询。失败则返回原查询。"""
    q = str(q or "").strip()[:400]
    if not q:
        return []
    prompt = _REWRITE_PROMPT.format(query=q)
    try:
        _models = models or []
        if not _models and harness:
            route = harness.resolve_model_route(q, {})
            _models = route.get("candidates") or [route.get("selected")]
        opts = {**options, "_skip_search": True, "_skip_quality": True}
        r, _ = await harness._ask_with_fallback(_models, prompt, opts, messages=None)
        if r and r.success and r.content:
            lines = [ln.strip() for ln in r.content.strip().splitlines() if ln.strip()]
            # 过滤掉可能的解释性行（超长或含中文标点多）
            cleaned = [ln[:400] for ln in lines if 4 <= len(ln) <= 400][:3]
            if cleaned:
                return cleaned
    except Exception:
        pass
    return [q[:500]]


_DECOMPOSE_PROMPT = """\
将以下问题拆解为 2-4 个独立的子问题，每个子问题可单独回答。
直接输出子问题列表，每行一个，不加编号，不加解释。

问题：{prompt}

子问题列表："""


async def decompose_query_llm(
    prompt: str,
    harness: Any,
    options: Dict[str, Any],
    hcfg: Dict[str, Any],
    *,
    models: Optional[List[str]] = None,
    max_subproblems: int = 4,
) -> List[str]:
    """LLM 驱动的子问题分解：将复杂问题拆为独立子查询。失败则返回原始问题。"""
    prompt = str(prompt or "").strip()[:600]
    if not prompt:
        return []
    llm_prompt = _DECOMPOSE_PROMPT.format(prompt=prompt)
    try:
        _models = models or []
        if not _models and harness:
            route = harness.resolve_model_route(prompt, {})
            _models = route.get("candidates") or [route.get("selected")]
        opts = {**options, "_skip_search": True, "_skip_quality": True}
        r, _ = await harness._ask_with_fallback(_models, llm_prompt, opts, messages=None)
        if r and r.success and r.content:
            lines = [ln.strip() for ln in r.content.strip().splitlines() if ln.strip()]
            cleaned = [ln[:400] for ln in lines if 6 <= len(ln) <= 400][:max_subproblems]
            if len(cleaned) >= 2:
                return cleaned
    except Exception:
        pass
    return [prompt[:500]]


def expand_queries(prompt: str, seeds: List[str], target_n: int) -> List[str]:
    out: List[str] = []
    seen = set()
    for s in seeds:
        r = rewrite_query(s)
        k = r.lower()
        if r and k not in seen:
            seen.add(k)
            out.append(r)
    base = str(prompt or "").strip().replace("\n", " ")[:180]
    candidates = []
    if base:
        candidates.append(f"{base} 要点")
        candidates.append(f"{base} 最新")
        candidates.append(f"{base} 官方")
    for c in candidates:
        if len(out) >= target_n:
            break
        k = c.lower()
        if k not in seen:
            seen.add(k)
            out.append(c[:400])
    return out[:target_n]


def dedup_queries(queries: List[str]) -> List[str]:
    seen = set()
    out: List[str] = []
    for q in queries:
        s = str(q or "").strip()
        k = s.lower()[:240]
        if s and k not in seen:
            seen.add(k)
            out.append(s)
    return out


def rerank_search_evidence(items: List[SearchEvidence], *, top_k: int = 48) -> List[SearchEvidence]:
    """轻量 rerank：trust * relevance * (0.5+freshness)，无需外部 reranker API。"""
    if not items:
        return []

    def score(e: SearchEvidence) -> float:
        return float(e.trust_score) * float(e.relevance_score) * (0.5 + float(e.freshness_score))

    ranked = sorted(items, key=score, reverse=True)
    return ranked[:top_k]


def prepare_parallel_queries(
    prompt: str,
    analysis: Dict[str, Any],
    *,
    n: int,
    entry_search_required: bool,
    search_reason: str,
    seed_builder: Callable[..., List[str]],
) -> List[str]:
    """seed_builder：与 ``dag_common.build_search_queries`` 签名兼容的可调用对象。"""
    raw = seed_builder(
        prompt,
        analysis,
        n=max(n * 2, 4),
        entry_search_required=entry_search_required,
        search_reason=search_reason or "",
    )
    expanded = expand_queries(prompt, raw, target_n=max(n * 2, n))
    return dedup_queries(expanded)[: max(1, n)]


async def prepare_parallel_queries_llm(
    prompt: str,
    analysis: Dict[str, Any],
    harness: Any,
    options: Dict[str, Any],
    hcfg: Dict[str, Any],
    *,
    n: int,
    entry_search_required: bool,
    search_reason: str,
    seed_builder: Callable[..., List[str]],
) -> List[str]:
    """LLM 强化版：复合问题先拆解为子问题，再改写首条种子，最后去重截取。"""
    # 基础种子
    raw = seed_builder(
        prompt,
        analysis,
        n=max(n * 2, 4),
        entry_search_required=entry_search_required,
        search_reason=search_reason or "",
    )
    expanded = expand_queries(prompt, raw, target_n=max(n * 2, n))
    seeds = dedup_queries(expanded)

    # Step 1: 复合问题子问题分解
    # 触发条件：ambiguity_score > 0.4 或 prompt 含 2+ 个问号
    ambiguity = float((analysis or {}).get("ambiguity_score") or 0.0)
    question_marks = prompt.count("?") + prompt.count("？")
    is_complex = ambiguity > 0.4 or question_marks >= 2 or len(prompt) > 200
    decomposed: List[str] = []
    if is_complex and harness:
        try:
            decomposed = await decompose_query_llm(prompt, harness, options, hcfg,
                                                   max_subproblems=min(n, 4))
        except Exception:
            pass

    # Step 2: LLM 改写首个种子（提精度）
    rewritten: List[str] = []
    if seeds and harness:
        try:
            rewritten = await rewrite_query_llm(seeds[0], harness, options, hcfg)
        except Exception:
            pass

    # Step 3: 合并优先级：子问题 > 改写结果 > 原始种子
    combined = dedup_queries((decomposed or []) + (rewritten or []) + seeds)
    return combined[: max(1, n)]
