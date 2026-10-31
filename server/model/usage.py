"""用量与健康记录（§7 / 附录 A：llm_calls / provider_health 表）。

落 SQLite（workbench/workbench.db），供用量页与健康页查询。
不入 git（工作区级，非真相源）。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from ..core.schema import LlmCallRecord

SCHEMA = """
CREATE TABLE IF NOT EXISTS llm_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    task TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    bridge TEXT DEFAULT '',
    prompt_tokens INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    total_tokens INTEGER DEFAULT 0,
    latency_ms INTEGER DEFAULT 0,
    ok INTEGER DEFAULT 1,
    error TEXT DEFAULT '',
    chapter INTEGER,
    cost_yuan REAL DEFAULT 0,
    fallback_hops INTEGER DEFAULT 0,
    stream INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_calls_ts ON llm_calls(ts);
CREATE INDEX IF NOT EXISTS idx_calls_task ON llm_calls(task);
CREATE INDEX IF NOT EXISTS idx_calls_provider ON llm_calls(provider);

CREATE TABLE IF NOT EXISTS provider_health (
    provider TEXT PRIMARY KEY,
    ok INTEGER NOT NULL,
    detail TEXT DEFAULT '',
    checked_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bridge_incidents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    provider TEXT NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT DEFAULT ''
);
"""


class UsageDB:
    """线程安全的 SQLite 用量库。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ---------------- 写入 ----------------
    def record(self, rec: LlmCallRecord) -> None:
        with self._lock, self._conn() as c:
            c.execute(
                """INSERT INTO llm_calls
                (ts, task, provider, model, bridge, prompt_tokens, completion_tokens,
                 total_tokens, latency_ms, ok, error, chapter, cost_yuan, fallback_hops, stream)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    rec.ts,
                    rec.task,
                    rec.provider,
                    rec.model,
                    rec.bridge,
                    rec.prompt_tokens,
                    rec.completion_tokens,
                    rec.total_tokens,
                    rec.latency_ms,
                    1 if rec.ok else 0,
                    rec.error,
                    rec.chapter,
                    rec.cost_yuan,
                    rec.fallback_hops,
                    1 if rec.stream else 0,
                ),
            )

    def record_health(self, provider: str, ok: bool, detail: str = "") -> None:
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT INTO provider_health(provider, ok, detail, checked_at) VALUES (?,?,?,?) "
                "ON CONFLICT(provider) DO UPDATE SET ok=excluded.ok, detail=excluded.detail, checked_at=excluded.checked_at",
                (provider, 1 if ok else 0, detail, _now()),
            )

    def record_incident(self, provider: str, kind: str, detail: str = "") -> None:
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT INTO bridge_incidents(ts, provider, kind, detail) VALUES (?,?,?,?)",
                (_now(), provider, kind, detail),
            )

    # ---------------- 查询 ----------------
    def health_snapshot(self) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM provider_health ORDER BY provider").fetchall()
        return [dict(r) for r in rows]

    def usage(self, days: int = 7) -> dict[str, Any]:
        since = time.strftime("%Y-%m-%dT00:00:00", time.localtime(time.time() - days * 86400))
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM llm_calls WHERE ts >= ? ORDER BY ts DESC", (since,)
            ).fetchall()
        calls = [dict(r) for r in rows]
        by_task: dict[str, dict[str, Any]] = {}
        by_provider: dict[str, dict[str, Any]] = {}
        by_day: dict[str, dict[str, Any]] = {}
        total = {"calls": 0, "tokens": 0, "prompt": 0, "completion": 0, "cost": 0.0, "errors": 0}
        for c in calls:
            t = by_task.setdefault(c["task"], {"calls": 0, "tokens": 0, "cost": 0.0, "errors": 0})
            p = by_provider.setdefault(c["provider"], {"calls": 0, "tokens": 0, "cost": 0.0, "errors": 0})
            d = by_day.setdefault(c["ts"][:10], {"calls": 0, "tokens": 0, "cost": 0.0})
            for bucket in (t, p, d):
                bucket["calls"] += 1
                bucket["tokens"] += c["total_tokens"] or 0
                if "cost" in bucket:
                    bucket["cost"] += c["cost_yuan"] or 0.0
                if "errors" in bucket and not c["ok"]:
                    bucket["errors"] += 1
            total["calls"] += 1
            total["tokens"] += c["total_tokens"] or 0
            total["prompt"] += c["prompt_tokens"] or 0
            total["completion"] += c["completion_tokens"] or 0
            total["cost"] += c["cost_yuan"] or 0.0
            if not c["ok"]:
                total["errors"] += 1
        # 订阅桥 vs 官方 API 占比（§13.2 用量页：订阅池占比）
        bridge_calls = sum(1 for c in calls if c["cost_yuan"] == 0 and c["provider"] != "deepseek-official")
        return {
            "days": days,
            "since": since,
            "total": total,
            "by_task": by_task,
            "by_provider": by_provider,
            "by_day": dict(sorted(by_day.items())),
            "recent": calls[:50],
            "subscription_share": (bridge_calls / len(calls)) if calls else 0.0,
        }

    def recent_incidents(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM bridge_incidents ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def chapter_cost(self, ch: int) -> dict[str, Any]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM llm_calls WHERE chapter = ?", (ch,)).fetchall()
        calls = [dict(r) for r in rows]
        return {
            "ch": ch,
            "calls": len(calls),
            "tokens": sum(r["total_tokens"] or 0 for r in calls),
            "cost": sum(r["cost_yuan"] or 0 for r in calls),
            "calls_detail": calls,
        }


def _now() -> str:
    import datetime as dt

    return dt.datetime.now().isoformat(timespec="seconds")
