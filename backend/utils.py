from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Tuple

import yaml


def now_ms() -> int:
    return int(time.time() * 1000)


def new_trace_id() -> str:
    return uuid.uuid4().hex


def load_yaml(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def env_get(name: str, default: str | None = None) -> str | None:
    v = os.getenv(name)
    return v if v is not None and v != "" else default


@dataclass
class Timer:
    start_ms: int

    @classmethod
    def start(cls) -> "Timer":
        return cls(start_ms=now_ms())

    def elapsed_ms(self) -> int:
        return now_ms() - self.start_ms


def clamp_int(x: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, x))


def pick_latency_ms(range_pair: Tuple[int, int]) -> int:
    # Small deterministic-ish jitter based on time
    lo, hi = range_pair
    if hi <= lo:
        return lo
    t = now_ms()
    return lo + (t % (hi - lo + 1))
