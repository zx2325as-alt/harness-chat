from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Request, BackgroundTasks, Depends
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
import json
from pydantic import BaseModel, Field

import redis

from harness import DualTrackHarness
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
    mode: str = Field(default="auto", description="auto | fast | refine")
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

    harness = DualTrackHarness(cfg)

    @app.get("/api/health")
    async def health() -> Dict[str, Any]:
        return {"ok": True}

    @app.get("/api/config")
    async def get_config() -> Dict[str, Any]:
        # expose safe subset for UI
        h = cfg.get("harness") or {}
        return {
            "harness": {
                "default_mode": h.get("default_mode", "auto"),
                "complexity": h.get("complexity", {}),
                "routing": h.get("routing", {}),
                "refine_chain": h.get("refine_chain", {}),
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
            
        last_prompt = messages[-1]["content"] if messages else ""
        if isinstance(last_prompt, list):
            last_prompt = "\n".join(c["text"] for c in last_prompt if c.get("type") == "text")
        
        result = await harness.run(str(last_prompt), messages=messages, mode=req.mode, options=options)
        return result

    @app.post("/api/chat/stream")
    async def chat_stream(req: ChatRequest, request: Request) -> Any:
        """流式 SSE 接口"""
        options = dict(req.options or {})
        trace_id = options.setdefault("trace_id", new_trace_id())
        
        session_id = req.session_id
        redis_key = f"chat_session:{session_id}" if session_id else None
        
        # Build context from Redis instead of Frontend (if enabled)
        historical_messages = []
        if redis_client and redis_key:
            try:
                cached_msgs = redis_client.lrange(redis_key, 0, -1)
                for m in cached_msgs:
                    historical_messages.append(json.loads(m))
            except Exception as e:
                print(f"Failed to load from redis: {e}")
        else:
            # Fallback to frontend-provided messages if no Redis
            historical_messages = [m.model_dump() for m in req.messages]
            
        # Prepare the current prompt
        # req.prompt 可能是空的，如果前端把问题放在了 req.messages 的最后一条
        current_prompt_content = req.prompt
        if not current_prompt_content and req.messages:
            current_prompt_content = req.messages[-1].content
            
        current_user_msg = {"role": "user", "content": current_prompt_content}
        
        # Determine the string representation of the prompt for routing/analysis
        last_prompt_str = current_prompt_content
        if isinstance(last_prompt_str, list):
            last_prompt_str = "\n".join(c.get("text", "") for c in last_prompt_str if c.get("type") == "text")
            
        if not last_prompt_str:
            return {"error": "No prompt or messages provided"}

        async def event_generator():
            final_answer = ""
            try:
                # This will run the harness in streaming mode. 
                # We need harness to yield events as it progresses.
                async for event in harness.run_stream(str(last_prompt_str), messages=historical_messages, mode=req.mode, options=options):
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

        return StreamingResponse(event_generator(), media_type="text/event-stream")

    return app


app = create_app()

