from __future__ import annotations

import argparse
import json
from typing import Any, Dict, List

from model_adapters import build_adapter
from utils import load_yaml


PROMPT = """给定用户问题和参考答案，提取 3-5 个必须覆盖的关键要点。
要求：
- 只输出 JSON 数组，例如：["要点1","要点2","要点3"]
- 要点要可判定、可核查，避免空话（如“回答清晰”）

问题：
{question}

参考答案：
{reference}
"""


def _pick_model(cfg: Dict[str, Any], key: str) -> str:
    if key:
        return key
    # 质量优先：默认用 generation.polish 的首选；否则 routing.default_model
    h = cfg.get("harness") or {}
    tpl = (h.get("task_model_templates") or {}).get("generation") or {}
    pm = ((tpl.get("quality_models") or {}).get("polish") or [])
    if isinstance(pm, list) and pm:
        return str(pm[0])
    return str((h.get("routing") or {}).get("default_model") or "gpt-5.5")


def _safe_json_array(text: str) -> List[str]:
    t = (text or "").strip()
    # 尝试直接解析
    try:
        obj = json.loads(t)
        if isinstance(obj, list):
            return [str(x).strip() for x in obj if str(x).strip()][:8]
    except Exception:
        pass
    # 尝试从文本中提取第一个 [...] 片段
    import re

    m = re.search(r"\[[\s\S]*\]", t)
    if m:
        try:
            obj = json.loads(m.group(0))
            if isinstance(obj, list):
                return [str(x).strip() for x in obj if str(x).strip()][:8]
        except Exception:
            pass
    return []


def main() -> int:
    ap = argparse.ArgumentParser(description="用 LLM 为离线评测集生成要点标注（3-5条）")
    ap.add_argument("--config", default="config.yaml", help="backend/config.yaml 路径（相对 backend 目录）")
    ap.add_argument("--in", dest="inp", default="eval_set_raw.json")
    ap.add_argument("--out", default="eval_set_labeled.json")
    ap.add_argument("--model-key", default="", help="用于标注的模型 key（默认自动选择）")
    ap.add_argument("--limit", type=int, default=200)
    args = ap.parse_args()

    cfg = load_yaml(args.config)
    mk = _pick_model(cfg, str(args.model_key or "").strip())
    models_cfg = cfg.get("models") or {}
    if mk not in models_cfg:
        raise SystemExit(f"model_key not registered in config.yaml models: {mk!r}")
    adapter = build_adapter(mk, models_cfg[mk])

    rows = json.load(open(args.inp, "r", encoding="utf-8"))
    if not isinstance(rows, list):
        raise SystemExit("input must be a json array")
    rows = rows[: max(1, int(args.limit))]

    out: List[Dict[str, Any]] = []
    for i, r in enumerate(rows, start=1):
        q = str((r or {}).get("question") or "")
        ref = str((r or {}).get("reference") or "")
        p = PROMPT.format(question=q[:4000], reference=ref[:8000])
        res = asyncio_run(adapter.ask(p, {"temperature": 0.0, "request_timeout_s": 60, "max_retries": 2}, messages=None))
        points = _safe_json_array(res.content if res and getattr(res, "success", False) else "")
        out.append({**(r or {}), "points": points, "label_model": mk, "label_ok": bool(points)})
        if i % 10 == 0:
            print(f"labeled {i}/{len(rows)}")

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"wrote {len(out)} rows -> {args.out}")
    return 0


def asyncio_run(coro):
    import asyncio

    try:
        loop = asyncio.get_event_loop()
    except Exception:
        loop = None
    if loop and loop.is_running():
        # 简单兼容：在已运行事件循环中不支持（脚本场景不应发生）
        raise RuntimeError("event loop is already running")
    return asyncio.run(coro)


if __name__ == "__main__":
    raise SystemExit(main())

