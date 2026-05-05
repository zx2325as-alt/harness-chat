from __future__ import annotations

import math
import re
from functools import lru_cache
from typing import Dict, Iterable, List


_EMBED_MODEL_NAME = "paraphrase-multilingual-MiniLM-L12-v2"


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
    try:
        from sentence_transformers import SentenceTransformer
    except Exception:
        return None
    try:
        # 默认只读本地缓存，避免在启动预热/首请求时阻塞等待 HuggingFace
        #（若本地没有模型，则直接回退到稀疏向量相似度）
        return SentenceTransformer(_EMBED_MODEL_NAME, local_files_only=True)
    except Exception:
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


async def warm_up_embedding_model() -> None:
    """
    预热 sentence-transformers：触发模型加载 + 一次最小 encode，
    避免首个文档问答请求在主路径上承担冷启动开销。
    """
    import asyncio

    def _warm_sync() -> None:
        m = _load_sentence_transformer()
        if m is None:
            return
        try:
            m.encode(["-"], normalize_embeddings=True)
        except Exception:
            return

    try:
        await asyncio.to_thread(_warm_sync)
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


def ngram_overlap_ratio(left: str, right: str) -> float:
    a = set(_tokenize(left))
    b = set(_tokenize(right))
    if not a or not b:
        return 0.0
    return len(a & b) / max(1, min(len(a), len(b)))
