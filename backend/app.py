from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Tuple

from fastapi import FastAPI, Request, UploadFile, File, Body
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
import json
import asyncio
import math
import re
from pydantic import BaseModel, Field

import redis

from harness import DualTrackHarness
from document_extract import extract_document
from utils import load_yaml, new_trace_id


ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(ROOT, "config.yaml")


# Initialize Redis connection
# Note: For production, parameters should come from config.yaml or env vars.
# For now, we assume a local redis instance.
try:
    redis_client = redis.Redis(host='localhost', port=6379, db=0, decode_responses=True)
    redis_client.ping()
except Exception as e:
    print(f"Warning: Failed to connect to Redis ({e}). Sessions will not be persisted.")
    redis_client = None

class Message(BaseModel):
    role: str
    content: Any

class ChatRequest(BaseModel):
    session_id: Optional[str] = None  # Added session_id for Redis tracking
    prompt: Any = Field(default="", description="The new user message")
    messages: List[Message] = Field(default_factory=list, description="Historical conversation messages")
    mode: str = Field(default="auto", description="auto | fast | refine | agent（agent 仅流式推荐；同步接口会降级为 refine）")
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


def _documents_context(documents: Any, query: str = "", max_total_chars: int = 60_000) -> str:
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


def _augment_prompt(prompt: str, options: Dict[str, Any]) -> str:
    docs_context = _documents_context(options.get("documents"), query=prompt)
    if not docs_context:
        return prompt
    return (
        f"{docs_context}\n\n"
        "请优先基于上述文档回答；涉及文档信息时，尽量标注来自哪份文档或哪段内容。\n\n"
        f"【用户问题】\n{prompt}"
    )


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


def create_app() -> FastAPI:
    cfg = load_yaml(CONFIG_PATH)
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
    async def parse_documents(files: List[UploadFile] = File(...)) -> Dict[str, Any]:
        documents = []
        for f in files:
            data = await f.read()
            name = f.filename or "未命名文件"

            def _sync_extract() -> Dict[str, Any]:
                return extract_document(name, data).to_dict()

            documents.append(await asyncio.to_thread(_sync_extract))
        return {"documents": documents}

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
            log_path = os.path.join(ROOT, "feedback.log")
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
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
        if not str(last_prompt).strip() and not hist:
            return {"error": "No prompt or messages provided"}
        options["search_prompt_base"] = str(last_prompt)
        augmented = _augment_prompt(str(last_prompt), options)

        result = await harness.run(augmented, messages=hist, mode=req.mode, options=options)
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
        if prefer_server_history and redis_client and redis_key:
            try:
                # 同步 Redis 会阻塞整个 asyncio 事件循环，导致 SSE 在首包发出前就卡死；放入线程并限时。
                cached_msgs = await asyncio.wait_for(
                    asyncio.to_thread(redis_client.lrange, redis_key, 0, -1),
                    timeout=2.5,
                )
                if cached_msgs:
                    historical_messages = [json.loads(m) for m in cached_msgs]
            except Exception as e:
                print(f"Redis history skipped (timeout/error): {e}")

        historical_messages, current_prompt_content = _split_current_from_history(
            historical_messages, req.prompt
        )

        current_user_msg = {"role": "user", "content": current_prompt_content}

        last_prompt_str = _content_to_text(current_prompt_content)
        if not str(last_prompt_str).strip() and isinstance(options.get("documents"), list) and options.get("documents"):
            last_prompt_str = "请根据上传的文档回答问题。"

        if not str(last_prompt_str).strip():
            return {"error": "No prompt or messages provided"}

        options["search_prompt_base"] = str(last_prompt_str)
        augmented_prompt = _augment_prompt(str(last_prompt_str), options)

        client_run_id = str(options.get("client_run_id") or "").strip()
        stream_connect_attempt = int(options.get("stream_connect_attempt") or 0)
        harness_options = {
            k: v for k, v in options.items() if k not in ("client_run_id", "stream_connect_attempt")
        }

        async def event_generator():
            final_answer = ""
            stream_failed = False
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
                        augmented_prompt,
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
                while True:
                    if await request.is_disconnected():
                        task.cancel()
                        break

                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=15)
                    except asyncio.TimeoutError:
                        yield f"data: {json.dumps({'event': 'heartbeat', 'trace_id': trace_id}, ensure_ascii=False)}\n\n"
                        continue

                    if event is None:
                        break

                    event = _normalise_stream_event(event)

                    # We need to capture the final content to store it in redis
                    if event.get("event") == "chunk":
                        data = event.get("data", {})
                        if "content" in data:
                            final_answer += data["content"]
                    if event.get("event") == "error":
                        stream_failed = True
                            
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                    if stream_failed:
                        break
                    
                # If everything succeeded and we have a final answer, update Redis
                if not stream_failed and redis_client and redis_key and final_answer:
                    try:
                        await asyncio.to_thread(redis_client.rpush, redis_key, json.dumps(current_user_msg))
                        await asyncio.to_thread(
                            redis_client.rpush,
                            redis_key,
                            json.dumps({"role": "assistant", "content": final_answer}),
                        )
                        await asyncio.to_thread(redis_client.expire, redis_key, 60 * 60 * 24 * 30)  # 30 days
                        yield f"data: {json.dumps({'event': 'history_stored', 'session_id': session_id}, ensure_ascii=False)}\n\n"
                    except Exception as e:
                        print(f"Failed to save to redis: {e}")
                        
                if not stream_failed:
                    yield "data: [DONE]\n\n"
            except Exception as e:
                yield f"data: {json.dumps({'event': 'error', 'error': str(e), 'error_code': 'SSE_GENERATOR_ERROR', 'trace_id': trace_id}, ensure_ascii=False)}\n\n"
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

