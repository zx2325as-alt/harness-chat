#!/usr/bin/env python3
"""本地冒烟：模块导入 + JSON 抽取 + 流式分块（无需 API Key）。"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def main() -> int:
    from json_utils import extract_balanced_json_object, strip_markdown_json_fence
    from stream_chunking import iter_chunk_spans

    raw = "```json\n{\"ok\":true}\n```"
    assert "true" in strip_markdown_json_fence(raw)
    bal = extract_balanced_json_object('x {"a":1} y')
    assert bal == '{"a":1}'
    spans = iter_chunk_spans("一二三四五六七八九十。abcdefghij", 8, smart=True)
    assert sum(e - s for s, e in spans) == len("一二三四五六七八九十。abcdefghij")

    import harness as h  # noqa: F401

    print("smoke_check: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
