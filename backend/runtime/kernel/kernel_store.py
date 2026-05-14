from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import asdict, is_dataclass
from typing import Any, Dict, Iterable, List, Optional

from runtime.kernel.kernel_models import CheckpointRef, KernelEvent


def json_ready(value: Any) -> Any:
    if is_dataclass(value):
        return json_ready(asdict(value))
    if isinstance(value, dict):
        return {str(k): json_ready(v) for k, v in value.items()}
    if isinstance(value, list):
        return [json_ready(v) for v in value]
    if isinstance(value, tuple):
        return [json_ready(v) for v in value]
    if isinstance(value, set):
        return [json_ready(v) for v in sorted(value, key=lambda x: str(x))]
    return value


def dumps_json(value: Any) -> str:
    return json.dumps(json_ready(value), ensure_ascii=False)


def loads_json(raw: Any, default: Any) -> Any:
    if raw in (None, ""):
        return default
    try:
        return json.loads(str(raw))
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


class KernelStore(ABC):
    @abstractmethod
    async def upsert_run(self, run_id: str, payload: Dict[str, Any]) -> None:
        raise NotImplementedError

    @abstractmethod
    async def load_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        raise NotImplementedError

    @abstractmethod
    async def append_event(self, event: KernelEvent) -> int:
        raise NotImplementedError

    @abstractmethod
    async def load_events(self, run_id: str, *, after_seq: int = 0) -> List[KernelEvent]:
        raise NotImplementedError

    @abstractmethod
    async def load_public_events(self, run_id: str, *, after_seq: int = 0) -> List[Dict[str, Any]]:
        raise NotImplementedError

    @abstractmethod
    async def save_checkpoint(
        self,
        run_id: str,
        *,
        seq: int,
        node_id: str,
        label: str,
        state: Dict[str, Any],
    ) -> CheckpointRef:
        raise NotImplementedError

    @abstractmethod
    async def load_latest_checkpoint(self, run_id: str) -> Optional[Dict[str, Any]]:
        raise NotImplementedError


async def load_public_event_tail(store: KernelStore, run_id: str, *, after_seq: int = 0) -> Iterable[Dict[str, Any]]:
    return await store.load_public_events(run_id, after_seq=after_seq)
