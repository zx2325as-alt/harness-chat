from __future__ import annotations

import asyncio
import os
import sqlite3
import threading
from typing import Any, Dict, List, Optional

from runtime.kernel.kernel_models import CheckpointRef, KernelEvent, now_ts_ms
from runtime.kernel.kernel_projectors import event_to_public_event
from runtime.kernel.kernel_store import KernelStore, dumps_json, json_ready, loads_json

_lock = threading.Lock()


def resolve_kernel_sqlite_path(hcfg: Dict[str, Any]) -> str:
    orch = hcfg.get("runtime_orchestrator") if isinstance(hcfg.get("runtime_orchestrator"), dict) else {}
    obs = orch.get("observability") if isinstance(orch.get("observability"), dict) else {}
    raw = str(obs.get("runtime_kernel_sqlite_path") or obs.get("kernel_sqlite_path") or "").strip()
    backend_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if not raw:
        raw = os.path.join("data", "runtime_kernel.sqlite")
    if os.path.isabs(raw):
        return raw
    return os.path.normpath(os.path.join(backend_dir, raw))


class SQLiteKernelStore(KernelStore):
    def __init__(self, path: str) -> None:
        self.path = path

    async def upsert_run(self, run_id: str, payload: Dict[str, Any]) -> None:
        await asyncio.to_thread(self._upsert_run_sync, run_id, payload)

    async def load_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        return await asyncio.to_thread(self._load_run_sync, run_id)

    async def append_event(self, event: KernelEvent) -> int:
        return await asyncio.to_thread(self._append_event_sync, event)

    async def load_events(self, run_id: str, *, after_seq: int = 0) -> List[KernelEvent]:
        return await asyncio.to_thread(self._load_events_sync, run_id, int(after_seq or 0))

    async def load_public_events(self, run_id: str, *, after_seq: int = 0) -> List[Dict[str, Any]]:
        return await asyncio.to_thread(self._load_public_events_sync, run_id, int(after_seq or 0))

    async def save_checkpoint(
        self,
        run_id: str,
        *,
        seq: int,
        node_id: str,
        label: str,
        state: Dict[str, Any],
    ) -> CheckpointRef:
        return await asyncio.to_thread(self._save_checkpoint_sync, run_id, int(seq or 0), node_id, label, state)

    async def load_latest_checkpoint(self, run_id: str) -> Optional[Dict[str, Any]]:
        return await asyncio.to_thread(self._load_latest_checkpoint_sync, run_id)

    def _connect(self) -> sqlite3.Connection:
        parent = os.path.dirname(os.path.abspath(self.path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS kernel_runs (
              run_id TEXT PRIMARY KEY,
              updated_ts_ms INTEGER NOT NULL,
              payload TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS kernel_events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              run_id TEXT NOT NULL,
              seq INTEGER NOT NULL,
              ts_ms INTEGER NOT NULL,
              event_type TEXT NOT NULL,
              node_id TEXT,
              phase TEXT,
              attempt INTEGER NOT NULL DEFAULT 0,
              payload TEXT NOT NULL,
              public_event TEXT,
              UNIQUE(run_id, seq)
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_kernel_events_run_seq ON kernel_events(run_id, seq)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS kernel_checkpoints (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              run_id TEXT NOT NULL,
              checkpoint_seq INTEGER NOT NULL,
              ts_ms INTEGER NOT NULL,
              node_id TEXT,
              label TEXT,
              state_json TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_kernel_ckpt_run_seq ON kernel_checkpoints(run_id, checkpoint_seq)")

    def _upsert_run_sync(self, run_id: str, payload: Dict[str, Any]) -> None:
        with _lock:
            conn = self._connect()
            try:
                self._ensure_schema(conn)
                conn.execute(
                    "INSERT INTO kernel_runs(run_id, updated_ts_ms, payload) VALUES (?, ?, ?) "
                    "ON CONFLICT(run_id) DO UPDATE SET updated_ts_ms=excluded.updated_ts_ms, payload=excluded.payload",
                    (run_id, now_ts_ms(), dumps_json(payload)),
                )
                conn.commit()
            finally:
                conn.close()

    def _load_run_sync(self, run_id: str) -> Optional[Dict[str, Any]]:
        with _lock:
            conn = self._connect()
            try:
                self._ensure_schema(conn)
                row = conn.execute("SELECT payload FROM kernel_runs WHERE run_id=?", (run_id,)).fetchone()
                return loads_json(row["payload"], None) if row else None
            finally:
                conn.close()

    def _append_event_sync(self, event: KernelEvent) -> int:
        with _lock:
            conn = self._connect()
            try:
                self._ensure_schema(conn)
                seq = int(event.seq or 0)
                if seq <= 0:
                    row = conn.execute("SELECT COALESCE(MAX(seq), 0) AS max_seq FROM kernel_events WHERE run_id=?", (event.run_id,)).fetchone()
                    seq = int((row["max_seq"] if row else 0) or 0) + 1
                    event.seq = seq
                conn.execute(
                    "INSERT OR REPLACE INTO kernel_events(run_id, seq, ts_ms, event_type, node_id, phase, attempt, payload, public_event) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        event.run_id,
                        seq,
                        int(event.ts_ms or now_ts_ms()),
                        str(event.event_type or ""),
                        str(event.node_id or ""),
                        str(event.phase or ""),
                        int(event.attempt or 0),
                        dumps_json(event.payload or {}),
                        dumps_json(event.public_event or {}) if isinstance(event.public_event, dict) else None,
                    ),
                )
                conn.commit()
                return seq
            finally:
                conn.close()

    def _load_events_sync(self, run_id: str, after_seq: int) -> List[KernelEvent]:
        with _lock:
            conn = self._connect()
            try:
                self._ensure_schema(conn)
                rows = conn.execute(
                    "SELECT run_id, seq, ts_ms, event_type, node_id, phase, attempt, payload, public_event FROM kernel_events "
                    "WHERE run_id=? AND seq>? ORDER BY seq ASC",
                    (run_id, after_seq),
                ).fetchall()
                out: List[KernelEvent] = []
                for row in rows:
                    out.append(
                        KernelEvent(
                            run_id=str(row["run_id"]),
                            trace_id="",
                            event_type=str(row["event_type"]),
                            seq=int(row["seq"] or 0),
                            ts_ms=int(row["ts_ms"] or 0),
                            node_id=str(row["node_id"] or ""),
                            phase=str(row["phase"] or ""),
                            attempt=int(row["attempt"] or 0),
                            payload=loads_json(row["payload"], {}),
                            public_event=loads_json(row["public_event"], None),
                        )
                    )
                return out
            finally:
                conn.close()

    def _load_public_events_sync(self, run_id: str, after_seq: int) -> List[Dict[str, Any]]:
        rows = self._load_events_sync(run_id, after_seq)
        out: List[Dict[str, Any]] = []
        for event in rows:
            public_event = event_to_public_event(event)
            if isinstance(public_event, dict):
                out.append(json_ready(public_event))
        return out

    def _save_checkpoint_sync(self, run_id: str, seq: int, node_id: str, label: str, state: Dict[str, Any]) -> CheckpointRef:
        ref = CheckpointRef(
            run_id=run_id,
            checkpoint_id="",
            seq=seq,
            ts_ms=now_ts_ms(),
            node_id=str(node_id or ""),
            label=str(label or "checkpoint"),
        )
        with _lock:
            conn = self._connect()
            try:
                self._ensure_schema(conn)
                cur = conn.execute(
                    "INSERT INTO kernel_checkpoints(run_id, checkpoint_seq, ts_ms, node_id, label, state_json) VALUES (?, ?, ?, ?, ?, ?)",
                    (run_id, seq, ref.ts_ms, ref.node_id, ref.label, dumps_json(state)),
                )
                ref.checkpoint_id = f"sqlite:{cur.lastrowid}"
                conn.commit()
                return ref
            finally:
                conn.close()

    def _load_latest_checkpoint_sync(self, run_id: str) -> Optional[Dict[str, Any]]:
        with _lock:
            conn = self._connect()
            try:
                self._ensure_schema(conn)
                row = conn.execute(
                    "SELECT id, checkpoint_seq, ts_ms, node_id, label, state_json FROM kernel_checkpoints WHERE run_id=? ORDER BY checkpoint_seq DESC, id DESC LIMIT 1",
                    (run_id,),
                ).fetchone()
                if not row:
                    return None
                return {
                    "ref": {
                        "run_id": run_id,
                        "checkpoint_id": f"sqlite:{int(row['id'])}",
                        "seq": int(row["checkpoint_seq"] or 0),
                        "ts_ms": int(row["ts_ms"] or 0),
                        "node_id": str(row["node_id"] or ""),
                        "label": str(row["label"] or ""),
                    },
                    "state": loads_json(row["state_json"], {}),
                }
            finally:
                conn.close()
