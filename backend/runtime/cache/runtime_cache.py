"""分层 Runtime 缓存：intent / evidence / draft / critic（内存 LRU 占位，可换 Redis）。"""
from __future__ import annotations

from collections import OrderedDict
from typing import Any, Dict, Hashable, Optional, Tuple, TypeVar

T = TypeVar("T")


class _LRU:
    def __init__(self, max_items: int = 64) -> None:
        self.max_items = max(8, max_items)
        self._d: "OrderedDict[Hashable, Any]" = OrderedDict()

    def get(self, key: Hashable) -> Optional[Any]:
        if key not in self._d:
            return None
        self._d.move_to_end(key)
        return self._d[key]

    def put(self, key: Hashable, value: Any) -> None:
        self._d[key] = value
        self._d.move_to_end(key)
        while len(self._d) > self.max_items:
            self._d.popitem(last=False)


class SemanticCache:
    def __init__(self) -> None:
        self._store = _LRU(48)

    def key(self, intent_blob: Tuple[Any, ...], context_sig: str) -> Tuple[str, str]:
        return ("semantic", f"{hash(intent_blob)}:{hash(context_sig)}")

    def get(self, intent_blob: Tuple[Any, ...], context_sig: str) -> Optional[str]:
        return self._store.get(self.key(intent_blob, context_sig))

    def put(self, intent_blob: Tuple[Any, ...], context_sig: str, text: str) -> None:
        self._store.put(self.key(intent_blob, context_sig), text)


class EvidenceCache:
    def __init__(self) -> None:
        self._store = _LRU(32)

    def get(self, query_key: str) -> Optional[Any]:
        return self._store.get(("ev", query_key))

    def put(self, query_key: str, payload: Any) -> None:
        self._store.put(("ev", query_key), payload)


class DraftCache:
    def __init__(self) -> None:
        self._store = _LRU(24)

    def get(self, draft_key: str) -> Optional[str]:
        return self._store.get(("dr", draft_key))

    def put(self, draft_key: str, text: str) -> None:
        self._store.put(("dr", draft_key), text)


class CriticCache:
    def __init__(self) -> None:
        self._store = _LRU(24)

    def get(self, critic_key: str) -> Optional[Dict[str, Any]]:
        val = self._store.get(("cr", critic_key))
        return val if isinstance(val, dict) else None

    def put(self, critic_key: str, merged: Dict[str, Any]) -> None:
        self._store.put(("cr", critic_key), merged)


class RuntimeTieredCaches:
    def __init__(self) -> None:
        self.semantic = SemanticCache()
        self.evidence = EvidenceCache()
        self.draft = DraftCache()
        self.critic = CriticCache()
