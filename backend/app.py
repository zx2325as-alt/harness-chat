from __future__ import annotations

import os
import sys
from pathlib import Path

# 确保无论 cwd / PYTHONPATH 如何，都能加载与本文件同目录下的 harness、semantic_utils 等模块
ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from typing import Any, Dict, List, Optional, Tuple

from fastapi import FastAPI, Request, UploadFile, File, Body, HTTPException, Form
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
import json
import asyncio
import math
import re
import hashlib
from pydantic import BaseModel, Field

import redis

from harness import DualTrackHarness
from document_extract import (
    SUPPORTED_DOCUMENT_EXTS,
    configure_document_limits,
    detect_document_ext,
    extract_document,
)
from local_docs import load_folder_documents
from semantic_utils import batch_semantic_similarity
from utils import load_yaml, new_trace_id, env_get


CONFIG_PATH = os.path.join(ROOT, "config.yaml")


redis_client = None
ALLOWED_DOCUMENT_EXTS = set(SUPPORTED_DOCUMENT_EXTS)
DEFAULT_MAX_UPLOAD_FILES = 6
DEFAULT_MAX_UPLOAD_BYTES = 10 * 1024 * 1024
DEFAULT_REDIS_HISTORY_ITEMS = 40

class Message(BaseModel):
    role: str
    content: Any

class ChatRequest(BaseModel):
    session_id: Optional[str] = None  # Added session_id for Redis tracking
    prompt: Any = Field(default="", description="The new user message")
    messages: List[Message] = Field(default_factory=list, description="Historical conversation messages")
    mode: str = Field(
        default="auto",
        description="auto | fast | refine | agent（当前真 Agent 循环仅在 /api/chat/stream 提供；/api/chat 选择 agent 时会自动降级为 refine）",
    )
    options: Dict[str, Any] = Field(default_factory=dict)


class StepOut(BaseModel):
    name: str
    status: str
    step_id: Optional[str] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    latency_ms: Optional[int] = None
    input_preview: Optional[str] = None
    output: Optional[str] = None
    error: Optional[str] = None
    meta: Dict[str, Any] = Field(default_factory=dict)


class AskOut(BaseModel):
    success: bool
    content: str
    provider: str
    model: str
    latency_ms: int
    tokens_in: int = 0
    tokens_out: int = 0
    error: Optional[str] = None
    raw: Optional[Dict[str, Any]] = None


class ChatResponse(BaseModel):
    trace_id: str
    track: str
    final: AskOut
    steps: List[StepOut]
    meta: Optional[Dict[str, Any]] = None


def _content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        return "\n".join(parts)
    return str(content or "")


def _doc_query_terms(query: str) -> List[str]:
    """查询词：英文/数字 token + 中文 2–4 字 n-gram，供轻量 BM25 打分。"""
    q = (query or "").strip().lower()
    if not q:
        return []
    terms: List[str] = []
    for tok in re.findall(r"[\w\u4e00-\u9fff]{2,}", q):
        if len(tok) <= 32:
            terms.append(tok)
    for n in (2, 3, 4):
        if len(q) >= n:
            for i in range(0, min(len(q), 400) - n + 1):
                g = q[i : i + n]
                if re.search(r"[\u4e00-\u9fff]", g):
                    terms.append(g)
    out: List[str] = []
    seen = set()
    for t in terms:
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out[:120]


def _doc_bm25_scores(query: str, docs: List[str], *, k1: float = 1.2, b: float = 0.75) -> List[float]:
    """无第三方依赖的 BM25；docs 为各 chunk 纯文本。"""
    terms = _doc_query_terms(query)
    if not terms:
        return [0.0] * len(docs)
    N = len(docs)
    df: Dict[str, int] = {t: 0 for t in terms}
    tfs: List[Dict[str, int]] = []
    dl: List[int] = []
    for d in docs:
        low = (d or "").lower()
        dl.append(len(low) or 1)
        tf: Dict[str, int] = {t: 0 for t in terms}
        for t in terms:
            c = low.count(t)
            if c:
                tf[t] = c
                df[t] += 1
        tfs.append(tf)
    avgdl = sum(dl) / max(N, 1)
    scores = []
    for i in range(N):
        s = 0.0
        denom_dl = k1 * (1 - b + b * dl[i] / avgdl)
        for t in terms:
            f = tfs[i].get(t, 0)
            if not f:
                continue
            dft = df.get(t, 0)
            idf = math.log(1.0 + (N - dft + 0.5) / (dft + 0.5))
            s += idf * (f * (k1 + 1)) / (f + denom_dl)
        scores.append(s)
    return scores


def _doc_score(query: str, text: str) -> int:
    """保留旧接口：非零表示与查询有 token 命中（用于兜底）。"""
    terms = _doc_query_terms(query)
    if not terms:
        return 0
    t = (text or "").lower()
    return sum(1 for tok in terms if tok in t)


def _documents_context(
    documents: Any,
    query: str = "",
    max_total_chars: int = 60_000,
    *,
    bm25_weight: float = 0.55,
    embedding_weight: float = 0.45,
) -> str:
    if not isinstance(documents, list) or not documents:
        return ""
    pieces = ["【已上传文档内容】"]
    used = 0
    ranked: List[Dict[str, Any]] = []
    for idx, doc in enumerate(documents, start=1):
        if not isinstance(doc, dict):
            continue
        name = doc.get("name") or f"文档{idx}"
        chunks = doc.get("chunks") or []
        if isinstance(chunks, list) and chunks:
            for c in chunks:
                if not isinstance(c, dict):
                    continue
                content = str(c.get("content") or "")
                if content:
                    ranked.append(
                        {
                            "name": name,
                            "content": content,
                            "score": 0,
                            "chunk": c.get("index"),
                        }
                    )
        else:
            content = str(doc.get("content") or "")
            if content:
                ranked.append({"name": name, "content": content, "score": 0, "chunk": None})
    if not ranked:
        return ""
    if query:
        texts = [str(r.get("content") or "") for r in ranked]
        bm = _doc_bm25_scores(query, texts)
        for i, r in enumerate(ranked):
            r["score"] = float(bm[i]) if i < len(bm) else 0.0
        ranked.sort(key=lambda x: x.get("score", 0), reverse=True)
        top_n = ranked[:20]
        emb_scores = batch_semantic_similarity(query, [str(r.get("content") or "")[:2000] for r in top_n])
        for idx, row in enumerate(top_n):
            row["embedding_score"] = float(emb_scores[idx]) if idx < len(emb_scores) else 0.0
            row["score"] = row.get("score", 0.0) * bm25_weight + row.get("embedding_score", 0.0) * embedding_weight
        for row in top_n:
            name = str(row.get("name") or "")
            head = str(row.get("content") or "")[:800]
            bonus = 0.0
            if query.strip():
                bonus = 0.028 * min(8, _doc_score(query, name) + _doc_score(query, head))
            row["score"] = float(row.get("score", 0.0)) + bonus
        top_n.sort(key=lambda x: x.get("score", 0), reverse=True)
        ranked = top_n + ranked[20:]
    selected = [r for r in ranked if float(r.get("score", 0) or 0) > 0][:8] if query else []
    if not selected:
        selected = ranked[:8]
    for idx, row in enumerate(selected, start=1):
        content = str(row.get("content") or "")
        if not content:
            continue
        remain = max_total_chars - used
        if remain <= 0:
            break
        clipped = content[:remain]
        used += len(clipped)
        chunk_label = f" · 片段 {row.get('chunk')}" if row.get("chunk") is not None else ""
        pieces.append(f"\n【文档 {idx}: {row.get('name')}{chunk_label}】\n{clipped}")
    if len(pieces) == 1:
        return ""
    return "\n".join(pieces)


def _int_option(options: Dict[str, Any], key: str, default: int, *, minimum: int, maximum: int) -> int:
    try:
        value = int(options.get(key, default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _prepare_documents_context_block(prompt: str, options: Dict[str, Any]) -> str:
    doc_budget = _int_option(options, "doc_context_chars", 40_000, minimum=2_000, maximum=60_000)
    bw = float(options.get("_doc_bm25_weight", 0.55))
    ew = float(options.get("_doc_embedding_weight", 0.45))
    s = bw + ew
    if s <= 0:
        bw, ew = 0.55, 0.45
    else:
        bw, ew = bw / s, ew / s
    return _documents_context(
        options.get("documents"),
        query=prompt,
        max_total_chars=doc_budget,
        bm25_weight=bw,
        embedding_weight=ew,
    )


def _ensure_non_empty_prompt(last_prompt: str, hist: List[Dict[str, Any]]) -> None:
    if str(last_prompt).strip() or hist:
        return
    raise HTTPException(status_code=400, detail="No prompt or messages provided")


def _capture_stream_text(final_answer: str, event: Dict[str, Any]) -> Tuple[str, bool]:
    evt = str(event.get("event") or "")
    if evt == "content_reset":
        return "", False
    if evt == "chunk":
        data = event.get("data", {})
        if "content" in data:
            final_answer += str(data["content"])
    return final_answer, evt == "error_terminal"


def _init_redis_client(cfg: Dict[str, Any]) -> Optional[redis.Redis]:
    redis_cfg = ((cfg.get("server") or {}).get("redis") or {})
    host = str(redis_cfg.get("host") or env_get("REDIS_HOST", "localhost"))
    port = int(redis_cfg.get("port") or env_get("REDIS_PORT", "6379") or 6379)
    db = int(redis_cfg.get("db") or env_get("REDIS_DB", "0") or 0)
    password = redis_cfg.get("password") or env_get("REDIS_PASSWORD")
    enabled = bool(redis_cfg.get("enabled", True))
    if not enabled:
        return None
    try:
        client = redis.Redis(host=host, port=port, db=db, password=password, decode_responses=True)
        client.ping()
        return client
    except Exception as e:
        print(f"Warning: Failed to connect to Redis ({e}). Sessions will not be persisted.")
        return None


def _validate_upload_name(filename: str) -> str:
    ext = detect_document_ext(filename)
    if ext not in ALLOWED_DOCUMENT_EXTS:
        raise HTTPException(status_code=400, detail=f"暂不支持的文件格式：.{ext or 'unknown'}")
    return ext


async def _read_upload_file_limited(upload: UploadFile, max_bytes: int) -> bytes:
    chunks: List[bytes] = []
    total = 0
    while True:
        chunk = await upload.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(status_code=413, detail=f"文件过大：{upload.filename or '未命名文件'}")
        chunks.append(chunk)
    return b"".join(chunks)


def _redis_history_limit(cfg: Dict[str, Any]) -> int:
    server_cfg = cfg.get("server") or {}
    return _int_option(
        server_cfg,
        "session_history_items",
        DEFAULT_REDIS_HISTORY_ITEMS,
        minimum=10,
        maximum=200,
    )


def _store_history(redis_conn: redis.Redis, key: str, user_msg: Dict[str, Any], answer: str, max_items: int) -> None:
    payload_user = json.dumps(user_msg, ensure_ascii=False)
    payload_assistant = json.dumps({"role": "assistant", "content": answer}, ensure_ascii=False)
    pipe = redis_conn.pipeline()
    pipe.rpush(key, payload_user, payload_assistant)
    pipe.ltrim(key, -max_items, -1)
    pipe.expire(key, 60 * 60 * 24 * 30)
    pipe.execute()


def _document_cache_key(data: bytes) -> str:
    return "harness:docparse:" + hashlib.sha256(data).hexdigest()


def _load_document_cache(redis_conn: Optional[redis.Redis], data: bytes) -> Optional[Dict[str, Any]]:
    if not redis_conn:
        return None
    try:
        raw = redis_conn.get(_document_cache_key(data))
        return json.loads(raw) if raw else None
    except Exception:
        return None


def _store_document_cache(redis_conn: Optional[redis.Redis], data: bytes, doc: Dict[str, Any]) -> None:
    if not redis_conn:
        return
    try:
        redis_conn.set(_document_cache_key(data), json.dumps(doc, ensure_ascii=False), ex=3600)
    except Exception:
        pass


def _append_feedback_line(log_path: str, line: str) -> None:
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def _step_id_for(step: Dict[str, Any]) -> str:
    name = str(step.get("name") or "step")
    meta = step.get("meta") if isinstance(step.get("meta"), dict) else {}
    if step.get("step_id"):
        return str(step["step_id"])
    if meta.get("step_id"):
        return str(meta["step_id"])
    pg = str(meta.get("phase_group") or meta.get("pipeline_phase") or "")
    if name == "agent_iteration" and meta.get("i") is not None:
        return f"{name}:{meta.get('i')}"
    if name == "review_web_search" and meta.get("review_round") is not None:
        return f"{name}:{pg}:{meta.get('review_round')}"
    if name == "agent_web_search" and meta.get("query"):
        return f"{name}:{meta.get('query')}"
    return f"{name}:{pg}" if pg else name


def _split_current_from_history(
    messages: List[Dict[str, Any]], prompt: Any
) -> Tuple[List[Dict[str, Any]], Any]:
    """
    历史 messages 不含「当前用户句」，避免与 harness 传入的 prompt 在适配器里重复拼接。
    当前句优先取 req.prompt；否则取 messages 最后一条 user 并从历史中移除。
    """
    hist = [dict(m) for m in (messages or [])]
    has_prompt = prompt not in (None, "", [], {})
    if has_prompt:
        cur_norm = _content_to_text(prompt).strip()
        while hist and hist[-1].get("role") == "user":
            last_norm = _content_to_text(hist[-1].get("content")).strip()
            if cur_norm and last_norm == cur_norm:
                hist.pop()
            else:
                break
        return hist, prompt
    if hist and hist[-1].get("role") == "user":
        last = hist.pop()
        return hist, last.get("content")
    return hist, ""


def _normalise_stream_event(event: Dict[str, Any]) -> Dict[str, Any]:
    if event.get("event") == "step" and isinstance(event.get("step"), dict):
        step = dict(event["step"])
        meta = dict(step.get("meta") or {})
        sid = _step_id_for(step)
        step["step_id"] = sid
        meta["step_id"] = sid
        if "parent_id" not in meta:
            meta["parent_id"] = meta.get("phase_group") or meta.get("pipeline_phase") or ""
        step["meta"] = meta
        event = {**event, "step": step}
    return event


def _validate_harness_models(cfg: Dict[str, Any]) -> None:
    """启动时校验配置中引用的模型名是否已在 models 注册（仅告警，不阻断）。"""
    models = set((cfg.get("models") or {}).keys())
    h = cfg.get("harness") or {}
    cx = h.get("complexity") or {}
    am = str(cx.get("analyzer_model") or "").strip()
    if am and am not in models:
        print(f"Warning: harness.complexity.analyzer_model not registered: {am!r}")
    sc = (h.get("search") or {}).get("relevance_filter") or {}
    rm = str(sc.get("model") or "").strip()
    if rm and rm not in models:
        print(f"Warning: harness.search.relevance_filter.model not registered: {rm!r}")
    rt = h.get("routing") or {}
    dm = str(rt.get("default_model") or "").strip()
    if dm and dm not in models:
        print(f"Warning: harness.routing.default_model not registered: {dm!r}")
    for mk in rt.get("default_models") or []:
        s = str(mk or "").strip()
        if s and s not in models:
            print(f"Warning: harness.routing.default_models contains unknown model: {s!r}")
    ag = h.get("agent") or {}
    am = str(ag.get("model") or "").strip()
    if am and am not in models:
        print(f"Warning: harness.agent.model not registered: {am!r}")
    for _tt, mk in (ag.get("model_by_task_type") or {}).items():
        s = str(mk or "").strip()
        if s and s not in models:
            print(f"Warning: harness.agent.model_by_task_type.{_tt} unknown model: {s!r}")
    tpl = h.get("task_model_templates") or {}
    for tt, block in tpl.items():
        if not isinstance(block, dict):
            continue
        sm = str(block.get("selected_model") or "").strip()
        if sm and sm not in models:
            print(f"Warning: task_model_templates.{tt}.selected_model not registered: {sm!r}")
        for fb in block.get("fallback_models") or []:
            s = str(fb or "").strip()
            if s and s not in models:
                print(f"Warning: task_model_templates.{tt}.fallback_models contains unknown model: {s!r}")
        rmd = block.get("refine_models") or {}
        if isinstance(rmd, dict):
            for pool in ("draft", "review", "polish"):
                for mk in rmd.get(pool) or []:
                    s = str(mk or "").strip()
                    if s and s not in models:
                        print(f"Warning: task_model_templates.{tt}.refine_models.{pool} unknown model: {s!r}")


def _warn_analyzer_timeout_budget(cfg: Dict[str, Any]) -> None:
    h = cfg.get("harness") or {}
    cpx = h.get("complexity") or {}
    try:
        req_t = int(cpx.get("analyzer_request_timeout_s", 20))
        retries = int(cpx.get("analyzer_max_retries", 1))
        total = int(cpx.get("analyzer_total_timeout_s", 45))
    except (TypeError, ValueError):
        return
    # OpenAICompatAdapter: for attempt in range(max_retries) → 共 max_retries 次 POST
    attempts = max(1, retries)
    floor = req_t * attempts
    if total < floor:
        print(
            f"Warning: harness.complexity.analyzer_total_timeout_s ({total}s) is below "
            f"analyzer_request_timeout_s × analyzer_max_retries = {floor}s; "
            "asyncio.wait_for may cancel the analyzer before it finishes."
        )


def create_app() -> FastAPI:
    global redis_client
    cfg = load_yaml(CONFIG_PATH)
    configure_document_limits((cfg.get("harness") or {}).get("documents"))
    _warn_analyzer_timeout_budget(cfg)
    _validate_harness_models(cfg)
    redis_client = _init_redis_client(cfg)
    app = FastAPI(title="Harness Chat (Dual-Track)", version="0.1.0")

    cors = (cfg.get("server") or {}).get("cors_allow_origins") or ["*"]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    harness = DualTrackHarness(cfg, redis_client=redis_client)

    @app.post("/api/documents/parse")
    async def parse_documents(
        files: List[UploadFile] = File(...),
        client_file_ids: Optional[List[str]] = Form(None),
    ) -> Dict[str, Any]:
        max_file_bytes = _int_option(
            cfg.get("server") or {},
            "upload_max_file_bytes",
            DEFAULT_MAX_UPLOAD_BYTES,
            minimum=256 * 1024,
            maximum=50 * 1024 * 1024,
        )
        documents = []
        ids = list(client_file_ids or [])
        for idx, f in enumerate(files):
            name = f.filename or "未命名文件"
            _validate_upload_name(name)
            data = await _read_upload_file_limited(f, max_file_bytes)
            client_file_id = ids[idx] if idx < len(ids) else None
            if redis_client:
                cached_doc = await asyncio.to_thread(_load_document_cache, redis_client, data)
            else:
                cached_doc = None
            if cached_doc:
                doc = dict(cached_doc)
                if client_file_id:
                    doc["client_file_id"] = client_file_id
                documents.append(doc)
                continue

            def _sync_extract() -> Dict[str, Any]:
                doc = extract_document(name, data).to_dict()
                _store_document_cache(redis_client, data, doc)
                if client_file_id:
                    doc["client_file_id"] = client_file_id
                return doc

            documents.append(await asyncio.to_thread(_sync_extract))
        return {"documents": documents}

    @app.post("/api/documents/parse_folder")
    async def parse_folder(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
        """
        读取服务器本地文件夹内所有支持格式的文档。
        payload:
          folder_path: str          必填，绝对或相对路径
          recursive:   bool = True  是否递归子目录
          max_files:   int  = 0     最多文件数；<= 0 表示不限制
        """
        folder_path = str(payload.get("folder_path") or "").strip()
        if not folder_path:
            raise HTTPException(status_code=400, detail="folder_path 不能为空")

        recursive = bool(payload.get("recursive", True))
        max_files = int(payload.get("max_files", 0) or 0)

        try:
            result = await load_folder_documents(
                folder_path,
                recursive=recursive,
                max_files=max_files,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"文件夹读取失败：{e}")

        return result

    @app.get("/api/health")
    async def health() -> Dict[str, Any]:
        return {"ok": True}

    @app.delete("/api/session/{session_id}/history")
    async def clear_session_history(session_id: str) -> Dict[str, Any]:
        """清空服务端 Redis 中该会话的上下文（与前端「清空上下文」配合）。"""
        if not redis_client or not session_id:
            return {"ok": True, "cleared": False, "reason": "no_redis_or_session"}
        key = f"chat_session:{session_id}"
        try:
            await asyncio.to_thread(redis_client.delete, key)
            return {"ok": True, "cleared": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    @app.post("/api/feedback")
    async def feedback(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
        """隐式反馈埋点（客户端可选调用）：复制/重生成等行为用于离线评估。"""
        ev = str(payload.get("event") or "unknown")
        meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
        line = json.dumps(
            {"event": ev, "session_id": payload.get("session_id"), "trace_id": payload.get("trace_id"), "meta": meta},
            ensure_ascii=False,
        )
        try:
            if redis_client:
                stream_payload = {
                    "event": ev,
                    "session_id": str(payload.get("session_id") or ""),
                    "trace_id": str(payload.get("trace_id") or ""),
                    "meta": json.dumps(meta, ensure_ascii=False),
                }
                await asyncio.to_thread(redis_client.xadd, "harness:feedback", stream_payload, maxlen=5000, approximate=True)
            log_path = os.path.join(ROOT, "feedback.log")
            await asyncio.to_thread(_append_feedback_line, log_path, line)
        except Exception as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True}

    @app.get("/api/config")
    async def get_config() -> Dict[str, Any]:
        # expose safe subset for UI
        h = cfg.get("harness") or {}
        ag = h.get("agent") or {}
        return {
            "harness": {
                "default_mode": h.get("default_mode", "auto"),
                "complexity": h.get("complexity", {}),
                "routing": h.get("routing", {}),
                "refine_chain": h.get("refine_chain", {}),
                "agent": {"enabled": bool(ag.get("enabled", True)), "max_iterations": ag.get("max_iterations", 5)},
            },
            "models": list((cfg.get("models") or {}).keys()),
        }

    @app.post("/api/chat", response_model=ChatResponse)
    async def chat(req: ChatRequest) -> Any:
        options = dict(req.options or {})
        options.setdefault("trace_id", new_trace_id())

        raw_msgs = [m.model_dump() for m in req.messages]
        hist, current = _split_current_from_history(raw_msgs, req.prompt)
        last_prompt = _content_to_text(current)
        if not str(last_prompt).strip() and isinstance(options.get("documents"), list) and options.get("documents"):
            last_prompt = "请根据上传的文档回答问题。"
        _ensure_non_empty_prompt(str(last_prompt), hist)
        options["search_prompt_base"] = str(last_prompt)
        options["session_id"] = req.session_id or ""
        hdoc = (cfg.get("harness") or {}).get("documents") or {}
        options["_doc_bm25_weight"] = float(hdoc.get("bm25_weight", 0.55))
        options["_doc_embedding_weight"] = float(hdoc.get("embedding_weight", 0.45))
        options["_documents_context_block"] = await asyncio.to_thread(
            _prepare_documents_context_block, str(last_prompt), options
        )
        result = await harness.run(str(last_prompt), messages=hist, mode=req.mode, options=options)
        return result

    @app.post("/api/chat/stream")
    async def chat_stream(req: ChatRequest, request: Request) -> Any:
        """流式 SSE 接口"""
        options = dict(req.options or {})
        trace_id = options.setdefault("trace_id", new_trace_id())
        
        session_id = req.session_id
        redis_key = f"chat_session:{session_id}" if session_id else None
        
        # 仅当前端明确选择服务端历史时才用 Redis 覆盖请求体，避免编辑/重生成后混入旧上下文。
        historical_messages = [m.model_dump() for m in req.messages]
        prefer_server_history = bool(options.get("prefer_server_history"))
        history_cache_hit = False
        if prefer_server_history and redis_client and redis_key:
            try:
                # 同步 Redis 会阻塞整个 asyncio 事件循环，导致 SSE 在首包发出前就卡死；放入线程并限时。
                cached_msgs = await asyncio.wait_for(
                    asyncio.to_thread(redis_client.lrange, redis_key, 0, -1),
                    timeout=2.5,
                )
                if cached_msgs:
                    historical_messages = [json.loads(m) for m in cached_msgs]
                    history_cache_hit = True
            except Exception as e:
                print(f"Redis history skipped (timeout/error): {e}")

        historical_messages, current_prompt_content = _split_current_from_history(
            historical_messages, req.prompt
        )

        current_user_msg = {"role": "user", "content": current_prompt_content}

        last_prompt_str = _content_to_text(current_prompt_content)
        if not str(last_prompt_str).strip() and isinstance(options.get("documents"), list) and options.get("documents"):
            last_prompt_str = "请根据上传的文档回答问题。"

        _ensure_non_empty_prompt(str(last_prompt_str), historical_messages)

        options["search_prompt_base"] = str(last_prompt_str)
        options["session_id"] = session_id or ""
        hdoc = (cfg.get("harness") or {}).get("documents") or {}
        options["_doc_bm25_weight"] = float(hdoc.get("bm25_weight", 0.55))
        options["_doc_embedding_weight"] = float(hdoc.get("embedding_weight", 0.45))
        options["_documents_context_block"] = await asyncio.to_thread(
            _prepare_documents_context_block, str(last_prompt_str), options
        )

        client_run_id = str(options.get("client_run_id") or "").strip()
        stream_connect_attempt = _int_option(options, "stream_connect_attempt", 0, minimum=0, maximum=20)
        harness_options = {
            k: v for k, v in options.items() if k not in ("client_run_id", "stream_connect_attempt")
        }
        redis_history_items = _redis_history_limit(cfg)
        try:
            sse_queue_timeout_s = float((cfg.get("server") or {}).get("sse_queue_timeout_s", 15))
        except (TypeError, ValueError):
            sse_queue_timeout_s = 15.0
        sse_queue_timeout_s = max(5.0, min(120.0, sse_queue_timeout_s))

        async def event_generator():
            final_answer = ""
            reset_pending = False
            stream_failed = False
            terminal_error: Optional[Dict[str, Any]] = None
            queue: asyncio.Queue = asyncio.Queue()

            async def _produce() -> None:
                lock_key: Optional[str] = None
                try:
                    if redis_client and session_id and client_run_id and stream_connect_attempt == 0:
                        lock_key = f"harness:sse_run:{session_id}:{client_run_id}"

                        def _try_lock() -> bool:
                            return bool(redis_client.set(lock_key, trace_id, nx=True, ex=1800))

                        got = await asyncio.to_thread(_try_lock)
                        if not got:
                            await queue.put(
                                {
                                    "event": "error",
                                    "error": "同一会话下该 client_run_id 已有进行中的流；请勿重复发起或稍后重试。",
                                    "error_code": "STREAM_CLIENT_RUN_ACTIVE",
                                    "trace_id": trace_id,
                                }
                            )
                            return
                    async for ev in harness.run_stream(
                        str(last_prompt_str),
                        messages=historical_messages,
                        mode=req.mode,
                        options=harness_options,
                    ):
                        await queue.put(ev)
                except Exception as exc:
                    await queue.put(
                        {
                            "event": "error",
                            "error": str(exc),
                            "error_code": "SERVER_STREAM_EXCEPTION",
                            "trace_id": trace_id,
                        }
                    )
                finally:
                    if lock_key and redis_client:
                        try:
                            await asyncio.to_thread(redis_client.delete, lock_key)
                        except Exception:
                            pass
                    await queue.put(None)

            task = asyncio.create_task(_produce())
            try:
                if prefer_server_history and redis_key and redis_client and not history_cache_hit:
                    yield f"data: {json.dumps({'event': 'history_miss', 'session_id': session_id, 'trace_id': trace_id}, ensure_ascii=False)}\n\n"
                while True:
                    if await request.is_disconnected():
                        task.cancel()
                        break

                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=sse_queue_timeout_s)
                    except asyncio.TimeoutError:
                        yield f"data: {json.dumps({'event': 'heartbeat', 'trace_id': trace_id}, ensure_ascii=False)}\n\n"
                        continue

                    if event is None:
                        break

                    event = _normalise_stream_event(event)

                    final_answer, event_failed = _capture_stream_text(final_answer, event)
                    if event.get("event") == "content_reset":
                        reset_pending = True
                    elif event.get("event") == "chunk" and (event.get("data") or {}).get("content"):
                        reset_pending = False
                    if event.get("event") == "error":
                        stream_failed = True
                        terminal_error = {
                            "event": "error_terminal",
                            "error": str(event.get("error") or "服务端流式处理失败"),
                            "error_code": str(event.get("error_code") or "STREAM_ERROR"),
                            "trace_id": str(event.get("trace_id") or trace_id),
                            "reset_pending": bool(reset_pending),
                        }
                    elif event_failed:
                        stream_failed = True

                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                    if stream_failed:
                        break
                    
                # If everything succeeded and we have a final answer, update Redis
                if not stream_failed and redis_client and redis_key and final_answer and not reset_pending:
                    try:
                        await asyncio.to_thread(
                            _store_history,
                            redis_client,
                            redis_key,
                            current_user_msg,
                            final_answer,
                            redis_history_items,
                        )
                        yield f"data: {json.dumps({'event': 'history_stored', 'session_id': session_id}, ensure_ascii=False)}\n\n"
                    except Exception as e:
                        print(f"Failed to save to redis: {e}")
                if stream_failed and terminal_error:
                    yield f"data: {json.dumps(terminal_error, ensure_ascii=False)}\n\n"
                yield "data: [DONE]\n\n"
            except Exception as e:
                err_event = {
                    "event": "error",
                    "error": str(e),
                    "error_code": "SSE_GENERATOR_ERROR",
                    "trace_id": trace_id,
                }
                yield f"data: {json.dumps(err_event, ensure_ascii=False)}\n\n"
                yield (
                    f"data: {json.dumps({'event': 'error_terminal', 'error': str(e), 'error_code': 'SSE_GENERATOR_ERROR', 'trace_id': trace_id, 'reset_pending': bool(reset_pending)}, ensure_ascii=False)}\n\n"
                )
                yield "data: [DONE]\n\n"
            finally:
                if not task.done():
                    task.cancel()

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    return app


app = create_app()
