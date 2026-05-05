from __future__ import annotations

import asyncio
import math
import re
from functools import lru_cache
from typing import Any, Dict, Iterable, List, Optional

import httpx

from utils import env_get


_DEFAULT_EMBED_MODEL = "text-embedding-3-large"
_DEFAULT_BASE_URL = "https://api.n1n.ai/v1"


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def is_probably_english(text: str, *, threshold: float = 0.2) -> bool:
    raw = normalize_text(text)
    if not raw:
        return False
    zh = len(re.findall(r"[\u4e00-\u9fff]", raw))
    letters = len(re.findall(r"[A-Za-z]", raw))
    total = max(len(raw), 1)
    if letters <= 0:
        return False
    return zh / total < threshold


def _tokenize(text: str) -> List[str]:
    low = normalize_text(text).lower()
    out: List[str] = []
    for tok in re.findall(r"[\w\u4e00-\u9fff]{2,}", low):
        out.append(tok)
    compact = re.sub(r"\s+", "", low)
    for n in (2, 3):
        if len(compact) >= n:
            for idx in range(0, min(len(compact), 300) - n + 1):
                gram = compact[idx : idx + n]
                if re.search(r"[A-Za-z\u4e00-\u9fff]", gram):
                    out.append(gram)
    return out


def _fallback_vector(text: str) -> Dict[str, float]:
    vec: Dict[str, float] = {}
    for tok in _tokenize(text):
        vec[tok] = vec.get(tok, 0.0) + 1.0
    return vec


def _cosine_sparse(a: Dict[str, float], b: Dict[str, float]) -> float:
    if not a or not b:
        return 0.0
    dot = 0.0
    for key, value in a.items():
        dot += value * b.get(key, 0.0)
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return dot / (na * nb)


@lru_cache(maxsize=1)
def _load_sentence_transformer():
    # 已弃用：不再使用线下 sentence-transformers。
    return None


def _embedding_vectors(texts: Iterable[str]):
    model = _load_sentence_transformer()
    if model is None:
        return None
    cleaned = [normalize_text(t) or "-" for t in texts]
    try:
        return model.encode(cleaned, normalize_embeddings=True)
    except Exception:
        return None


async def warm_up_embedding_model(*, model_cfg: Optional[Dict[str, Any]] = None) -> None:
    """
    预热线上 Embeddings：触发一次最小 /v1/embeddings 调用，
    避免首个文档问答请求承担 DNS/TLS/冷连接开销。
    """
    try:
        await embed_texts(["-"], model_cfg=model_cfg)
    except Exception:
        return


def semantic_similarity(left: str, right: str) -> float:
    emb = _embedding_vectors([left, right])
    if emb is not None and len(emb) == 2:
        try:
            return float(sum(float(a) * float(b) for a, b in zip(emb[0], emb[1])))
        except Exception:
            pass
    return _cosine_sparse(_fallback_vector(left), _fallback_vector(right))


def batch_semantic_similarity(query: str, texts: List[str]) -> List[float]:
    if not texts:
        return []
    emb = _embedding_vectors([query] + list(texts))
    if emb is not None and len(emb) == len(texts) + 1:
        qv = emb[0]
        scores: List[float] = []
        for row in emb[1:]:
            try:
                scores.append(float(sum(float(a) * float(b) for a, b in zip(qv, row))))
            except Exception:
                scores.append(0.0)
        return scores
    qv = _fallback_vector(query)
    return [_cosine_sparse(qv, _fallback_vector(text)) for text in texts]


_EMBED_LIMITS = httpx.Limits(max_connections=50, max_keepalive_connections=20, keepalive_expiry=60.0)
_embed_client: Optional[httpx.AsyncClient] = None
_embed_sem: Optional[asyncio.Semaphore] = None


def _get_embed_client() -> httpx.AsyncClient:
    global _embed_client
    if _embed_client is None or _embed_client.is_closed:
        _embed_client = httpx.AsyncClient(limits=_EMBED_LIMITS)
    return _embed_client


def _get_embed_semaphore() -> asyncio.Semaphore:
    global _embed_sem
    # 纯线上 embeddings：限制并发，避免瞬时峰值导致 429 → 全链降级
    if _embed_sem is None:
        _embed_sem = asyncio.Semaphore(8)
    return _embed_sem


def _openai_embeddings_url(base_url: str) -> str:
    u = (base_url or "").strip().rstrip("/")
    if not u:
        u = _DEFAULT_BASE_URL
    if not u.endswith("/v1"):
        u = u + "/v1"
    return u + "/embeddings"


def _get_api_key_from_model_cfg(cfg: Dict[str, Any]) -> str:
    api_key = str(cfg.get("api_key") or "").strip()
    if api_key:
        return api_key
    api_key_env = str(cfg.get("api_key_env") or "").strip()
    if api_key_env and (api_key_env.startswith("sk-") or len(api_key_env) > 30):
        return api_key_env
    if api_key_env:
        return str(env_get(api_key_env) or "").strip()
    return ""


async def embed_texts(
    texts: List[str],
    *,
    model_cfg: Optional[Dict[str, Any]] = None,
    timeout_s: float = 30.0,
) -> List[List[float]]:
    """
    纯线上 embeddings：OpenAI 兼容 /v1/embeddings。
    返回 vectors（与 texts 等长）；失败则返回空列表，调用方自行回退。
    """
    cfg = dict(model_cfg or {})
    base_url = str(cfg.get("base_url") or _DEFAULT_BASE_URL)
    url = _openai_embeddings_url(base_url)
    model = str(cfg.get("model") or _DEFAULT_EMBED_MODEL)
    api_key = _get_api_key_from_model_cfg(cfg)
    headers: Dict[str, str] = {"Content-Type": "application/json", "Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    cleaned = [normalize_text(t) or "-" for t in (texts or [])]
    if not cleaned:
        return []
    body = {"model": model, "input": cleaned}
    client = _get_embed_client()
    async with _get_embed_semaphore():
        r = await client.post(url, headers=headers, json=body, timeout=timeout_s)
    r.raise_for_status()
    data = r.json()
    rows = data.get("data")
    if not isinstance(rows, list):
        return []
    out: List[List[float]] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        vec = item.get("embedding")
        if isinstance(vec, list) and vec:
            try:
                out.append([float(x) for x in vec])
            except Exception:
                out.append([])
        else:
            out.append([])
    return out if len(out) == len(cleaned) else out[: len(cleaned)]


def cosine_dense(a: List[float], b: List[float]) -> float:
    if not a or not b:
        return 0.0
    n = min(len(a), len(b))
    if n <= 0:
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for i in range(n):
        av = float(a[i])
        bv = float(b[i])
        dot += av * bv
        na += av * av
        nb += bv * bv
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


async def batch_semantic_similarity_online(
    query: str,
    texts: List[str],
    *,
    model_cfg: Optional[Dict[str, Any]] = None,
    timeout_s: float = 30.0,
) -> List[float]:
    if not texts:
        return []
    try:
        vecs = await embed_texts([query] + list(texts), model_cfg=model_cfg, timeout_s=timeout_s)
    except Exception:
        vecs = []
    if vecs and len(vecs) >= 1:
        qv = vecs[0]
        dvs = vecs[1:]
        if qv and len(dvs) == len(texts):
            return [cosine_dense(qv, dv) for dv in dvs]
    # 回退：稀疏向量相似度（纯本地计算，不依赖模型）
    qv2 = _fallback_vector(query)
    return [_cosine_sparse(qv2, _fallback_vector(t)) for t in texts]


def ngram_overlap_ratio(left: str, right: str) -> float:
    a = set(_tokenize(left))
    b = set(_tokenize(right))
    if not a or not b:
        return 0.0
    return len(a & b) / max(1, min(len(a), len(b)))
