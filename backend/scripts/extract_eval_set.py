from __future__ import annotations

import argparse
import json
from typing import Any, Dict, List

import redis


def _decode(v: Any) -> str:
    if isinstance(v, (bytes, bytearray)):
        try:
            return v.decode("utf-8")
        except Exception:
            return v.decode("utf-8", errors="ignore")
    return str(v)


def main() -> int:
    ap = argparse.ArgumentParser(description="从 Redis 会话历史抽取离线评测集（真实问答对）")
    ap.add_argument("--redis-url", default="redis://localhost:6379/0")
    ap.add_argument("--pattern", default="chat_session:*", help="会话 key 模式（默认 chat_session:*）")
    ap.add_argument("--limit-sessions", type=int, default=300)
    ap.add_argument("--limit-pairs", type=int, default=2000)
    ap.add_argument("--out", default="eval_set_raw.json")
    args = ap.parse_args()

    r = redis.Redis.from_url(args.redis_url)
    keys = list(r.scan_iter(match=args.pattern, count=2000))[: max(1, int(args.limit_sessions))]
    pairs: List[Dict[str, Any]] = []

    for key in keys:
        if len(pairs) >= int(args.limit_pairs):
            break
        k = _decode(key)
        try:
            raw = r.lrange(key, 0, -1)
        except Exception:
            continue
        msgs = []
        for m in raw:
            try:
                msgs.append(json.loads(_decode(m)))
            except Exception:
                continue
        # 按 user+assistant 成对抽取
        for i in range(0, len(msgs) - 1):
            if len(pairs) >= int(args.limit_pairs):
                break
            a = msgs[i]
            b = msgs[i + 1]
            if not (isinstance(a, dict) and isinstance(b, dict)):
                continue
            if a.get("role") == "user" and b.get("role") == "assistant":
                q = a.get("content")
                ref = b.get("content")
                if not str(q or "").strip() or not str(ref or "").strip():
                    continue
                pairs.append(
                    {
                        "question": q,
                        "reference": ref,
                        "session": k,
                        "idx_in_session": i,
                    }
                )

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(pairs, f, ensure_ascii=False, indent=2)
    print(f"wrote {len(pairs)} pairs -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

