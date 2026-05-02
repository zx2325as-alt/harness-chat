from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Request, UploadFile, File, Body
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
import json
import asyncio
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


def _documents_context(documents: Any, max_total_chars: int = 60_000) -> str:
    if not isinstance(documents, list) or not documents:
        return ""
    pieces = ["【已上传文档内容】"]
    used = 0
    for idx, doc in enumerate(documents, start=1):
        if not isinstance(doc, dict):
            continue
        name = doc.get("name") or f"文档{idx}"
        content = str(doc.get("content") or "")
        if not content:
            chunks = doc.get("chunks") or []
            if isinstance(chunks, list):
                content = "\n\n".join(str(c.get("content", "")) for c in chunks if isinstance(c, dict))
        if not content:
            continue
        remain = max_total_chars - used
        if remain <= 0:
            break
        clipped = content[:remain]
        used += len(clipped)
        pieces.append(f"\n【文档 {idx}: {name}】\n{clipped}")
    if len(pieces) == 1:
        return ""
    return "\n".join(pieces)


def _augment_prompt(prompt: str, options: Dict[str, Any]) -> str:
    docs_context = _documents_context(options.get("documents"))
    if not docs_context:
        return prompt
    return (
        f"{docs_context}\n\n"
        "请优先基于上述文档回答；涉及文档信息时，尽量标注来自哪份文档或哪段内容。\n\n"
        f"【用户问题】\n{prompt}"
    )


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
            documents.append(extract_document(f.filename or "未命名文件", data).to_dict())
        return {"documents": documents}

    @app.get("/api/health")
    async def health() -> Dict[str, Any]:
        return {"ok": True}

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
        
        messages = [m.model_dump() for m in req.messages]
        if req.prompt:
            messages.append({"role": "user", "content": req.prompt})
            
        if not messages:
            return {"error": "No prompt or messages provided"}
            
        last_prompt = _content_to_text(messages[-1]["content"] if messages else "")
        if not str(last_prompt).strip() and isinstance(options.get("documents"), list) and options.get("documents"):
            last_prompt = "请根据上传的文档回答问题。"
        options["search_prompt_base"] = str(last_prompt)
        augmented = _augment_prompt(str(last_prompt), options)

        result = await harness.run(augmented, messages=messages, mode=req.mode, options=options)
        return result

    @app.post("/api/chat/stream")
    async def chat_stream(req: ChatRequest, request: Request) -> Any:
        """流式 SSE 接口"""
        options = dict(req.options or {})
        trace_id = options.setdefault("trace_id", new_trace_id())
        
        session_id = req.session_id
        redis_key = f"chat_session:{session_id}" if session_id else None
        
        # Build context from Redis instead of Frontend (if enabled)
        historical_messages = [m.model_dump() for m in req.messages]
        if redis_client and redis_key:
            try:
                # 同步 Redis 会阻塞整个 asyncio 事件循环，导致 SSE 在首包发出前就卡死；放入线程并限时。
                cached_msgs = await asyncio.wait_for(
                    asyncio.to_thread(redis_client.lrange, redis_key, 0, -1),
                    timeout=2.5,
                )
                historical_messages = [json.loads(m) for m in cached_msgs]
            except Exception as e:
                print(f"Redis history skipped (timeout/error): {e}")
            
        # Prepare the current prompt
        # req.prompt 可能是空的，如果前端把问题放在了 req.messages 的最后一条
        current_prompt_content = req.prompt
        if not current_prompt_content and req.messages:
            current_prompt_content = req.messages[-1].content
            
        current_user_msg = {"role": "user", "content": current_prompt_content}
        
        last_prompt_str = _content_to_text(current_prompt_content)
        if not str(last_prompt_str).strip() and isinstance(options.get("documents"), list) and options.get("documents"):
            last_prompt_str = "请根据上传的文档回答问题。"

        if not str(last_prompt_str).strip():
            return {"error": "No prompt or messages provided"}

        options["search_prompt_base"] = str(last_prompt_str)
        augmented_prompt = _augment_prompt(str(last_prompt_str), options)

        async def event_generator():
            final_answer = ""
            try:
                async for event in harness.run_stream(
                    augmented_prompt, messages=historical_messages, mode=req.mode, options=options
                ):
                    if await request.is_disconnected():
                        break
                        
                    # We need to capture the final content to store it in redis
                    if event.get("event") == "chunk":
                        data = event.get("data", {})
                        if "content" in data:
                            final_answer += data["content"]
                            
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                    
                # If everything succeeded and we have a final answer, update Redis
                if redis_client and redis_key and final_answer:
                    try:
                        redis_client.rpush(redis_key, json.dumps(current_user_msg))
                        redis_client.rpush(redis_key, json.dumps({"role": "assistant", "content": final_answer}))
                        redis_client.expire(redis_key, 60 * 60 * 24 * 30)  # 30 days
                    except Exception as e:
                        print(f"Failed to save to redis: {e}")
                        
                yield "data: [DONE]\n\n"
            except Exception as e:
                yield f"data: {json.dumps({'event': 'error', 'error': str(e)}, ensure_ascii=False)}\n\n"

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

