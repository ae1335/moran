"""SQLite 检索索引（§8.1 索引结构 + §8.5 更新策略）。

落盘位置：``store.p.rag_index / "index.db"``（``.rag-index/`` 已在 gitignore 里，
**整个库随时可删可重建**，真相源永远是 ``truth/`` + ``chapters/`` + ``memory/``）。

表结构
------
- ``chunks(chunk_id PK, ch, offset, text, hash, para_start, para_end, chars)``
- ``chunks_fts``  FTS5 虚表 ``(tokens, chunk_id UNINDEXED, ch UNINDEXED)``，
  ``tokenize='unicode61'``；中文由本模块预切成「单字 + 双字」token 串再写入，
  查询同样预处理，因此 FTS5 的 BM25 直接可用于中文。
- ``vectors(chunk_id PK, vec BLOB)``  float32 + L2 归一化。
- ``entities(entity_id PK, kind, ch, payload)``
- ``edges(id PK, src, rel, dst, ch, weight)``
- ``meta(key PK, value)``

版本策略
--------
``meta["schema_version"]`` 与 ``SCHEMA_VERSION`` 不符、或缺表、或库文件不存在，
``IndexBuilder.index_ready()`` 一律返回 ``False``（**不抛异常**），
上层据此降级到 ``mode="off"``。
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from server.core.paths import ch_name
from server.core.schema import EntityDelta
from server.core.store import NovelStore

from .chunker import Chunk, chunk_chapter, count_chars, hash_text
from .embedder import Embedder, pack_vector, run_coro_sync

# 缺陷 #59：索引重建的正文总量上界（切块代价超线性，见 _chapter_texts docstring）
MAX_INDEX_CHARS = 3_000_000


class IndexTooLargeError(RuntimeError):
    """正文总量超出索引上界。"""

#: 索引 schema 版本。改了表结构/分词方式就 +1，旧库会被判定为未就绪并自动重建。
SCHEMA_VERSION = 1

#: 索引库文件名
INDEX_DB = "index.db"

#: FTS5 分词配置（中文预处理后交给 unicode61 切空格）
FTS_TOKENIZE = "unicode61"

_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_ASCII_RE = re.compile(r"[A-Za-z0-9_]+")


# ================================================================== 分词


def cjk_tokens(text: str, *, unigrams: bool = True) -> list[str]:
    """中文 token 化：每个汉字成单字 token，连续汉字段额外生成双字 token。

    英文/数字按词保留（小写化）。返回的 token 列表直接以空格 join 后喂 FTS5。
    """
    if not text:
        return []
    tokens: list[str] = []
    run: list[str] = []

    def flush() -> None:
        if not run:
            return
        seg = "".join(run)
        if unigrams or len(seg) == 1:
            tokens.extend(seg)
        if len(seg) > 1:
            tokens.extend(seg[i : i + 2] for i in range(len(seg) - 1))
        run.clear()

    pos = 0
    n = len(text)
    while pos < n:
        ch = text[pos]
        if _CJK_RE.match(ch):
            run.append(ch)
            pos += 1
            continue
        flush()
        m = _ASCII_RE.match(text, pos)
        if m:
            tokens.append(m.group(0).lower())
            pos = m.end()
        else:
            pos += 1
    flush()
    return tokens


def tokenize_for_fts(text: str) -> str:
    """把原文转成写入/查询 FTS5 的 token 串（空格分隔）。"""
    return " ".join(cjk_tokens(text))


def _quote(token: str) -> str:
    return '"' + token.replace('"', "") + '"'


def build_match_expr(
    groups: Sequence[Sequence[str]],
    *,
    mode: str = "all",
) -> str:
    """把若干「同义组」拼成 FTS5 MATCH 表达式。

    每组内部 OR，组之间 AND（``mode="all"``）或 OR（``mode="any"``）。
    组为空则被跳过；全部为空返回 ``""``（调用方应放弃 FTS 通道）。
    """
    parts: list[str] = []
    for group in groups:
        toks = [t for t in dict.fromkeys(group) if t]
        if not toks:
            continue
        parts.append("(" + " OR ".join(_quote(t) for t in toks) + ")")
    if not parts:
        return ""
    joiner = " AND " if mode == "all" else " OR "
    return joiner.join(parts)


def _query_tokens(text: str) -> list[str]:
    """查询侧 token：单字 + 双字都进 MATCH（召回优先）。

    单字会带来噪声（如「体」命中「身体」），所以 search 层还会用
    :func:`query_phrases` 返回的双字词做一次精度过滤。
    """
    return cjk_tokens(text)


def query_phrases(text: str) -> list[str]:
    """查询的「强 token」（双字及以上），用于结果精度过滤。"""
    return cjk_tokens(text, unigrams=False)


def query_match_expr(
    text: str = "",
    terms: Iterable[str] = (),
    entities: Iterable[str] = (),
    *,
    mode: str = "any",
) -> str:
    """从查询三要素构造 MATCH 表达式。

    - ``text``：整句，切成 token 后成为**一个大 OR 组**（任一 token 命中即可）；
    - ``terms`` / ``entities``：每个词条各自成组，保证不同词条之间是 AND 语义
      （``mode="all"`` 时）。
    """
    groups: list[list[str]] = []
    base = _query_tokens(text)
    if base:
        groups.append(base)
    for t in terms:
        toks = _query_tokens(str(t))
        if toks:
            groups.append(toks)
    for e in entities:
        toks = _query_tokens(str(e))
        if toks:
            groups.append(toks)
    return build_match_expr(groups, mode=mode)


# ================================================================== 数据类型


@dataclass
class ChunkRow:
    chunk_id: str
    ch: int
    offset: int
    text: str
    hash: str
    para_start: int = 0
    para_end: int = 0
    chars: int = 0

    @classmethod
    def from_chunk(cls, c: Chunk, offset: int | None = None) -> "ChunkRow":
        return cls(
            chunk_id=c.chunk_id,
            ch=c.ch,
            offset=c.offset if offset is None else offset,
            text=c.text,
            hash=c.hash,
            para_start=c.para_start,
            para_end=c.para_end,
            chars=c.chars or count_chars(c.text),
        )


@dataclass
class IndexStats:
    chapters: int = 0
    chunks: int = 0
    entities: int = 0
    edges: int = 0
    vectors: int = 0
    version: int = 0
    built_at: str = ""
    db_path: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "chapters": self.chapters,
            "chunks": self.chunks,
            "entities": self.entities,
            "edges": self.edges,
            "vectors": self.vectors,
            "version": self.version,
            "built_at": self.built_at,
            "db_path": self.db_path,
        }


@dataclass
class BuildReport:
    """``build()`` 的返回。``changed`` 为实际重切的章号列表。"""

    full: bool = False
    changed: list[int] = field(default_factory=list)
    removed: list[int] = field(default_factory=list)
    embedded: int = 0
    entities: int = 0
    edges: int = 0
    elapsed_ms: int = 0
    stats: IndexStats = field(default_factory=IndexStats)
    error: str = ""
    #: 缺陷 #118：entity-deltas.jsonl 读不利索时的中文说明（空串 = 没问题）
    entity_source: str = ""
    #: 缺陷 #127：embedding 没跑成时的中文说明（空串 = 没问题）。
    # 实测外部 embedding 服务挂掉时 build 仍报 chunks=12/vectors=0/error=
    # 空串，检索悄悄退回 BM25，作者和 cron 谁都不知道。
    vector_source: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "full": self.full,
            "changed": list(self.changed),
            "removed": list(self.removed),
            "embedded": self.embedded,
            "entities": self.entities,
            "edges": self.edges,
            "elapsed_ms": self.elapsed_ms,
            "error": self.error,
            "entity_source": self.entity_source,
            "vector_source": self.vector_source,
            **self.stats.as_dict(),
        }


@dataclass
class DriftReport:
    """``verify()`` 的返回（§8.5 夜间 hash 对账）。"""

    ok: bool = True
    drifted: list[int] = field(default_factory=list)
    missing: list[int] = field(default_factory=list)
    orphaned: list[int] = field(default_factory=list)
    db_path: str = ""
    checked_at: str = ""
    error: str = ""
    #: 缺陷 #118：entity-deltas.jsonl 读不利索时的中文说明（空串 = 没问题）
    entity_source: str = ""
    #: 缺陷 #127：索引库里没有向量（embedding 没跑成）时的中文说明。
    vector_source: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "drifted": list(self.drifted),
            "missing": list(self.missing),
            "orphaned": list(self.orphaned),
            "drift": list(self.drifted),
            "db_path": self.db_path,
            "checked_at": self.checked_at,
            "error": self.error,
            "entity_source": self.entity_source,
            "vector_source": self.vector_source,
        }


# ================================================================== 表定义

_SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS chunks (
        chunk_id   TEXT PRIMARY KEY,
        ch         INTEGER NOT NULL,
        offset     INTEGER NOT NULL DEFAULT 0,
        text       TEXT NOT NULL,
        hash       TEXT NOT NULL,
        para_start INTEGER NOT NULL DEFAULT 0,
        para_end   INTEGER NOT NULL DEFAULT 0,
        chars      INTEGER NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_chunks_ch ON chunks(ch)",
    # 每章的聚合 hash，供增量比对用（比逐 chunk 比对便宜）
    """
    CREATE TABLE IF NOT EXISTS chapter_hashes (
        ch        INTEGER PRIMARY KEY,
        src_hash  TEXT NOT NULL,
        chunks    INTEGER NOT NULL DEFAULT 0,
        indexed_at TEXT NOT NULL DEFAULT ''
    )
    """,
    f"""
    CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
        tokens,
        chunk_id UNINDEXED,
        ch UNINDEXED,
        tokenize='{FTS_TOKENIZE}'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS vectors (
        chunk_id TEXT PRIMARY KEY,
        dim      INTEGER NOT NULL DEFAULT 0,
        vec      BLOB NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS entities (
        entity_id TEXT PRIMARY KEY,
        kind      TEXT NOT NULL DEFAULT '',
        ch        INTEGER NOT NULL DEFAULT 0,
        payload   TEXT NOT NULL DEFAULT '{}'
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_entities_kind ON entities(kind)",
    """
    CREATE TABLE IF NOT EXISTS edges (
        id     INTEGER PRIMARY KEY AUTOINCREMENT,
        src    TEXT NOT NULL,
        rel    TEXT NOT NULL,
        dst    TEXT NOT NULL,
        ch     INTEGER NOT NULL DEFAULT 0,
        weight REAL NOT NULL DEFAULT 1.0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src)",
    "CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst)",
    """
    CREATE TABLE IF NOT EXISTS meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL DEFAULT ''
    )
    """,
]

_REQUIRED_TABLES = {"chunks", "chunks_fts", "entities", "edges", "meta", "chapter_hashes", "vectors"}


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


# ================================================================== 构建器


class IndexBuilder:
    """索引构建/维护。**所有只读查询都不抛**，写操作失败抛 ``IndexError_`` 由上层吞。"""

    def __init__(self, store: NovelStore, embedder: Embedder | None = None):
        self.store = store
        self.embedder = embedder
        #: 缺陷 #118：最近一次实体重建时，entity-deltas.jsonl 的问题说明（空串 = 没问题）
        self._entity_source = ""
        #: 缺陷 #127：最近一次 embedding 失败的原因（空串 = 没失败）
        self._embed_error = ""

    # ---------------------------------------------------------- 路径/连接

    @property
    def db_path(self) -> Path:
        return Path(self.store.p.rag_index) / INDEX_DB

    def connect(self, *, create: bool = True) -> sqlite3.Connection:
        """打开索引库。

        缺陷 #128：只读分支原先只用 ``mode=ro``。WAL 模式的库哪怕以只读方式打开，
        SQLite 也会在库旁边新建 ``-wal`` / ``-shm`` 两个旁文件（实测：只读连接一开，
        目录里立刻多出 32KB 的 ``index.db-shm``）。正文一个字节没动，但任何按
        「目录下文件清单 + md5」做基线的校验都会因此变红 —— 假红一次，这个
        校验就没人信了（和 #51 的假绿同样致命）。

        ``immutable=1`` 告诉 SQLite 这个文件此刻不会变：不加锁、不建旁文件。
        实测读到的内容一致，且库文件字节前后相同。写连接不受影响。
        """
        path = self.db_path
        if create:
            path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(path))
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            return conn
        if not path.exists():
            raise FileNotFoundError(str(path))
        conn = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def _tx(self, *, create: bool = True) -> Iterator[sqlite3.Connection]:
        conn = self.connect(create=create)
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        for stmt in _SCHEMA:
            conn.execute(stmt)
        conn.execute(
            "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )

    # ---------------------------------------------------------- 就绪判断

    def index_ready(self) -> bool:
        """索引存在且 schema 版本匹配 → ``True``。**任何异常都折成 False**。"""
        try:
            path = self.db_path
            if not path.exists() or path.stat().st_size == 0:
                return False
            conn = self.connect(create=False)
            try:
                rows = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
                ).fetchall()
                names = {r[0] for r in rows}
                if not _REQUIRED_TABLES.issubset(names):
                    return False
                row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
                if row is None:
                    return False
                return int(row[0]) == SCHEMA_VERSION
            finally:
                conn.close()
        except Exception:
            return False

    #: facade / 上层用的别名
    ready = index_ready

    def schema_version(self) -> int:
        try:
            with self._tx(create=False) as conn:
                row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
                return int(row[0]) if row else 0
        except Exception:
            return 0

    # ---------------------------------------------------------- 元数据

    def _get_meta(self, conn: sqlite3.Connection, key: str, default: str = "") -> str:
        try:
            row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            return str(row[0]) if row else default
        except sqlite3.Error:
            return default

    def _set_meta(self, conn: sqlite3.Connection, key: str, value: str) -> None:
        conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )

    # ---------------------------------------------------------- 建库

    def ensure(self) -> bool:
        """建库（幂等）。返回是否新建了 schema。"""
        try:
            with self._tx() as conn:
                self._ensure_schema(conn)
            return True
        except Exception:
            return False

    def build(self, full: bool = False) -> BuildReport:
        """全量或增量构建。

        增量判定：``chapter_hashes.src_hash`` 与当前正文 ``hash_text`` 不一致的章
        才重切；正文已不存在的章会被删除。``full=True`` 时清空后全量重切。
        """
        t0 = time.time()
        report = BuildReport(full=bool(full))
        try:
            with self._tx() as conn:
                self._ensure_schema(conn)
                if full:
                    conn.execute("DELETE FROM chunks_fts")
                    conn.execute("DELETE FROM chunks")
                    conn.execute("DELETE FROM vectors")
                    conn.execute("DELETE FROM chapter_hashes")
                    old: dict[int, str] = {}
                else:
                    old = {
                        int(r["ch"]): str(r["src_hash"])
                        for r in conn.execute("SELECT ch, src_hash FROM chapter_hashes")
                    }

                current = self._chapter_texts()
                seen: set[int] = set()
                for ch in sorted(current):
                    text = current[ch]
                    seen.add(ch)
                    src_hash = hash_text(text)
                    if not full and old.get(ch) == src_hash:
                        continue
                    self._write_chapter(conn, ch, text, src_hash)
                    report.changed.append(ch)

                stale = sorted(set(old) - seen)
                for ch in stale:
                    self._delete_chapter(conn, ch)
                    report.removed.append(ch)

                report.embedded = self._embed_pending(conn)
                ent, edge = self._rebuild_entities(conn)
                report.entities, report.edges = ent, edge
                # 缺陷 #118：实体缩水必须看得见，不能悄悄少一半
                report.entity_source = self._entity_source

                self._set_meta(conn, "schema_version", str(SCHEMA_VERSION))
                self._set_meta(conn, "built_at", _now())
                self._set_meta(conn, "embedder", self._embedder_name())
                report.stats = self._stats(conn)
                # 缺陷 #127：向量没跑成不能只体现在 embedded=0 上
                report.vector_source = self._vector_source(report.stats)
        except Exception as exc:  # noqa: BLE001 - 构建失败要让上层能降级
            report.error = f"{type(exc).__name__}: {exc}"
            report.stats = self.stats()
            report.vector_source = self._vector_source(report.stats)
        report.elapsed_ms = int((time.time() - t0) * 1000)
        return report

    def _chapter_texts(self) -> dict[int, str]:
        """取全部正文字节。

        缺陷 #59：正文总量要有上界。切块的实测代价是超线性的（单章 200KB→0.9s、
        500KB→5.5s、1MB→23s），不设限的话一次索引重建就能把服务卡住几十秒——
        而索引重建挂在夜间定时任务里，卡住就没人看见。超限时直接抛，
        让上层 facade 走它既有的「失败返 error dict」降级路径，不静默截断正文。
        """
        out: dict[int, str] = {}
        total = 0
        for ch in self.store.list_chapters():
            try:
                text = self.store.read_chapter(ch)
            except Exception:
                continue
            if text and text.strip():
                out[int(ch)] = text
                total += len(text)
        if total > MAX_INDEX_CHARS:
            raise IndexTooLargeError(
                f"正文总量 {total} 字超出索引上界 {MAX_INDEX_CHARS} 字"
            )
        return out

    def _write_chapter(self, conn: sqlite3.Connection, ch: int, text: str, src_hash: str) -> int:
        """重写一章的 chunks（先删后建），返回 chunk 数。"""
        self._delete_chapter(conn, ch)
        chunks = chunk_chapter(ch, text)
        for c in chunks:
            conn.execute(
                "INSERT INTO chunks(chunk_id, ch, offset, text, hash, para_start, para_end, chars) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (c.chunk_id, c.ch, c.offset, c.text, c.hash, c.para_start, c.para_end,
                 c.chars or count_chars(c.text)),
            )
            conn.execute(
                "INSERT INTO chunks_fts(tokens, chunk_id, ch) VALUES(?,?,?)",
                (tokenize_for_fts(c.text), c.chunk_id, c.ch),
            )
        conn.execute(
            "INSERT INTO chapter_hashes(ch, src_hash, chunks, indexed_at) VALUES(?,?,?,?) "
            "ON CONFLICT(ch) DO UPDATE SET src_hash=excluded.src_hash, "
            "chunks=excluded.chunks, indexed_at=excluded.indexed_at",
            (ch, src_hash, len(chunks), _now()),
        )
        return len(chunks)

    def _delete_chapter(self, conn: sqlite3.Connection, ch: int) -> int:
        rows = conn.execute("SELECT chunk_id FROM chunks WHERE ch=?", (ch,)).fetchall()
        ids = [r[0] for r in rows]
        for cid in ids:
            conn.execute("DELETE FROM chunks_fts WHERE chunk_id=?", (cid,))
            conn.execute("DELETE FROM vectors WHERE chunk_id=?", (cid,))
        conn.execute("DELETE FROM chunks WHERE ch=?", (ch,))
        conn.execute("DELETE FROM chapter_hashes WHERE ch=?", (ch,))
        return len(ids)

    def add_chapter(self, ch: int) -> int:
        """单章增量更新（§8.5：章 final 后调用）。返回 chunk 数，失败返回 -1。"""
        try:
            text = self.store.read_chapter(ch)
        except Exception:
            return -1
        if not text or not text.strip():
            return self.remove_chapter(ch)
        try:
            with self._tx() as conn:
                self._ensure_schema(conn)
                n = self._write_chapter(conn, int(ch), text, hash_text(text))
                self._embed_pending(conn)
                self._set_meta(conn, "built_at", _now())
            return n
        except Exception:
            return -1

    def remove_chapter(self, ch: int) -> int:
        """删章。返回删除的 chunk 数；库不可用时返回 0。"""
        try:
            with self._tx(create=self.db_path.exists()) as conn:
                n = self._delete_chapter(conn, int(ch))
                self._set_meta(conn, "built_at", _now())
            return n
        except Exception:
            return 0

    # ---------------------------------------------------------- 向量

    def _embedder_name(self) -> str:
        if self.embedder is None:
            return ""
        return f"{getattr(self.embedder, 'name', 'embedder')}:{getattr(self.embedder, 'dim', 0)}"

    def _vector_source(self, stats: IndexStats) -> str:
        """缺陷 #127：把「向量缺失」翻译成一句中文。空串 = 没问题。

        两种成因要分开说，否则作者看到的都是同一句没用的话：
        - embedding 真跑挂了（有 embedder、没向量）→ 带异常原文，能直接定位；
        - 压根没配 embedder（无向量是预期）→ 只陈述事实，不说「失败」。
        """
        if stats.chunks <= 0:
            return ""
        if stats.vectors > 0:
            return ""
        if self._embed_error:
            return f"向量缺失：{stats.chunks} 个切块一个向量都没有（embedding 失败：{self._embed_error}）"
        if self.embedder is None:
            return (
                f"向量缺失：{stats.chunks} 个切块一个向量都没有"
                "（未配置 embedding，检索只有 BM25，语义召回不成立）"
            )
        return f"向量缺失：{stats.chunks} 个切块一个向量都没有"

    def _vector_breaks_ok(self, stats: IndexStats) -> bool:
        """零向量要不要算「对账不通过」。

        分两类，否则校验永远红、没人再看（#51 的教训反过来同样成立）：
        - 配了 embedder 却没有向量（embedding 跑挂 / 向量被清）→ 算失败；
        - 压根没配 embedder → 是配置选择，只提示，不判失败。
        """
        if stats.chunks <= 0 or stats.vectors > 0:
            return False
        return bool(self._embed_error) or self.embedder is not None

    def _embed_pending(self, conn: sqlite3.Connection, batch_size: int = 32) -> int:
        """给还没向量的 chunk 补 embedding。没 embedder 时直接返回 0。"""
        if self.embedder is None:
            return 0
        self._embed_error = ""
        try:
            dim = int(getattr(self.embedder, "dim", 0) or 0)
            rows = conn.execute(
                "SELECT c.chunk_id, c.text FROM chunks c "
                "LEFT JOIN vectors v ON v.chunk_id = c.chunk_id "
                "WHERE v.chunk_id IS NULL"
            ).fetchall()
            if not rows:
                return 0
            done = 0
            vec_dim = dim
            for i in range(0, len(rows), batch_size):
                batch = rows[i : i + batch_size]
                vecs = run_coro_sync(self.embedder.embed([r["text"] for r in batch]))
                for row, vec in zip(batch, vecs):
                    blob = pack_vector(vec)
                    vec_dim = len(vec) if vec else vec_dim
                    conn.execute(
                        "INSERT INTO vectors(chunk_id, dim, vec) VALUES(?,?,?) "
                        "ON CONFLICT(chunk_id) DO UPDATE SET dim=excluded.dim, vec=excluded.vec",
                        (row["chunk_id"], int(vec_dim), blob),
                    )
                    done += 1
            self._set_meta(conn, "vector_dim", str(vec_dim))
            return done
        except Exception as exc:  # noqa: BLE001
            # 缺陷 #127：embedding 挂了确实不影响 BM25 主链路，但不能一声不吭。
            # 记下来，由 build() 带进报告、verify() 并入 ok，作者和 cron 才看得见。
            self._embed_error = f"{type(exc).__name__}: {exc}"
            return 0

    def embed_all(self) -> int:
        """显式补齐所有向量（换 embedder 后调用）。返回补齐条数。"""
        if self.embedder is None:
            return 0
        try:
            with self._tx() as conn:
                self._ensure_schema(conn)
                conn.execute("DELETE FROM vectors")
                return self._embed_pending(conn)
        except Exception:
            return 0

    # ---------------------------------------------------------- 实体/边

    def rebuild_entities(self) -> dict[str, int]:
        """重建 entities / edges。返回 ``{"entities": n, "edges": m}``。"""
        try:
            with self._tx() as conn:
                self._ensure_schema(conn)
                ent, edge = self._rebuild_entities(conn)
                self._set_meta(conn, "built_at", _now())
            return {"entities": ent, "edges": edge}
        except Exception:
            return {"entities": 0, "edges": 0}

    def _rebuild_entities(self, conn: sqlite3.Connection) -> tuple[int, int]:
        """``truth/characters/*.yaml`` 静态实体 + ``memory/entity-deltas.jsonl`` 动态边。

        还顺带把 ``world/*.md`` 立成 ``kind="world"`` 的实体（GraphRAG 可检索世界观）。
        """
        conn.execute("DELETE FROM entities")
        conn.execute("DELETE FROM edges")
        ent = 0
        edge = 0

        # --- 静态：角色卡 ---
        char_ids: set[str] = set()
        try:
            cards = self.store.list_characters()
        except Exception:
            cards = []
        for card in cards:
            cid = str(getattr(card, "id", "") or getattr(card, "name", ""))
            if not cid:
                continue
            char_ids.add(cid)
            payload = card.model_dump(mode="json") if hasattr(card, "model_dump") else dict(card)
            conn.execute(
                "INSERT INTO entities(entity_id, kind, ch, payload) VALUES(?,?,?,?) "
                "ON CONFLICT(entity_id) DO UPDATE SET kind=excluded.kind, ch=excluded.ch, "
                "payload=excluded.payload",
                (f"chars/{cid}", "character", int(getattr(card, "first_chapter", 0) or 0),
                 json.dumps(payload, ensure_ascii=False)),
            )
            ent += 1
            # 静态关系（CharacterCard.relations）
            for rel in getattr(card, "relations", []) or []:
                target = str(getattr(rel, "target", "") or "").strip()
                rtype = str(getattr(rel, "type", "") or "").strip() or "related"
                if not target:
                    continue
                conn.execute(
                    "INSERT INTO edges(src, rel, dst, ch, weight) VALUES(?,?,?,?,?)",
                    (f"chars/{cid}", rtype, f"chars/{_bare_id(target)}",
                     int(getattr(card, "first_chapter", 0) or 0), 1.0),
                )
                edge += 1
            # aliases 立成可检索的软边（别名 → 本体）
            for alias in getattr(card, "aliases", []) or []:
                alias = str(alias).strip()
                if not alias or alias == cid:
                    continue
                conn.execute(
                    "INSERT INTO edges(src, rel, dst, ch, weight) VALUES(?,?,?,?,?)",
                    (f"chars/{_bare_id(alias)}", "alias_of", f"chars/{cid}", 0, 1.0),
                )
                edge += 1

        # --- 静态：世界观文件 ---
        try:
            world_names = self.store.list_world_files()
        except Exception:
            world_names = []
        for name in world_names:
            try:
                text = self.store.read_truth_text(f"world/{name}")
            except Exception:
                continue
            if not text.strip():
                continue
            stem = Path(name).stem
            conn.execute(
                "INSERT INTO entities(entity_id, kind, ch, payload) VALUES(?,?,?,?) "
                "ON CONFLICT(entity_id) DO UPDATE SET kind=excluded.kind, ch=excluded.ch, "
                "payload=excluded.payload",
                (f"world/{stem}", "world", 0,
                 json.dumps({"name": stem, "file": name, "excerpt": text.strip()[:400]},
                            ensure_ascii=False)),
            )
            ent += 1

        # --- 动态：entity-deltas.jsonl ---
        # 缺陷 #118：坏行照样跳过（一行坏数据不该让整次重建失败），但要把跳过了
        # 什么如实报给调用方——实测整文件非 JSON 时实体从 13 掉到 4 而 verify 全绿。
        try:
            _rep = self.store.read_entity_deltas_report()
            deltas: list[EntityDelta] = _rep.rows
            self._entity_source = _rep.problem("实体增量 entity-deltas.jsonl")
        except Exception:
            deltas = []
            self._entity_source = "实体增量 entity-deltas.jsonl 读不出来，其中的内容未参与索引"
        for d in deltas:
            ent_id = _bare_id(str(getattr(d, "entity", "") or ""))
            if not ent_id:
                continue
            kind = str(getattr(d, "kind", "") or "relation")
            ch = int(getattr(d, "ch", 0) or 0)
            op = str(getattr(d, "op", "") or "add")
            target = _bare_id(str(getattr(d, "target", "") or ""))
            rtype = str(getattr(d, "type", "") or "").strip()
            note = str(getattr(d, "note", "") or "")

            if op != "remove":
                conn.execute(
                    "INSERT INTO entities(entity_id, kind, ch, payload) VALUES(?,?,?,?) "
                    "ON CONFLICT(entity_id) DO UPDATE SET kind=excluded.kind, ch=excluded.ch, "
                    "payload=excluded.payload",
                    (f"chars/{ent_id}", kind, ch,
                     json.dumps({"id": ent_id, "kind": kind, "note": note}, ensure_ascii=False)),
                )
                ent += 1
            else:
                conn.execute("DELETE FROM entities WHERE entity_id=?", (f"chars/{ent_id}",))

            if target and rtype:
                conn.execute(
                    "INSERT INTO edges(src, rel, dst, ch, weight) VALUES(?,?,?,?,?)",
                    (f"chars/{ent_id}", rtype, f"chars/{target}", ch, 1.0),
                )
                edge += 1
                # 反向补一条，保证一跳邻居对称（关系本身有向，但检索要双向可达）
                conn.execute(
                    "INSERT INTO edges(src, rel, dst, ch, weight) VALUES(?,?,?,?,?)",
                    (f"chars/{target}", f"inv:{rtype}", f"chars/{ent_id}", ch, 0.5),
                )
                edge += 1

        return ent, edge

    # ---------------------------------------------------------- 统计/校验

    def _stats(self, conn: sqlite3.Connection) -> IndexStats:
        def one(sql: str) -> int:
            try:
                row = conn.execute(sql).fetchone()
                return int(row[0]) if row and row[0] is not None else 0
            except sqlite3.Error:
                return 0

        built_at = self._get_meta(conn, "built_at")
        version = int(self._get_meta(conn, "schema_version", "0") or 0)
        return IndexStats(
            chapters=one("SELECT COUNT(DISTINCT ch) FROM chunks"),
            chunks=one("SELECT COUNT(*) FROM chunks"),
            entities=one("SELECT COUNT(*) FROM entities"),
            edges=one("SELECT COUNT(*) FROM edges"),
            vectors=one("SELECT COUNT(*) FROM vectors"),
            version=version,
            built_at=built_at,
            db_path=str(self.db_path),
        )

    def stats(self) -> IndexStats:
        """索引统计。库不可用时返回全零（**不抛**）。"""
        try:
            with self._tx(create=False) as conn:
                return self._stats(conn)
        except Exception:
            return IndexStats(db_path=str(self.db_path))

    def verify(self) -> DriftReport:
        """hash 对账（§8.5 夜间校验）。

        - ``drifted``：正文 hash 与索引 ``src_hash`` 不一致的章（被改过但没重索引）
        - ``missing``：正文存在但索引里没有的章
        - ``orphaned``：索引里有但正文已删的章

        索引库打不开时 ``ok=False`` + ``error`` 说明原因，**不抛**。
        """
        report = DriftReport(db_path=str(self.db_path), checked_at=_now())
        try:
            if not self.db_path.exists():
                report.ok = False
                report.error = "index_missing"
                return report
            current = self._chapter_texts()
            with self._tx(create=False) as conn:
                rows = conn.execute("SELECT ch, src_hash FROM chapter_hashes").fetchall()
            indexed = {int(r["ch"]): str(r["src_hash"]) for r in rows}
            for ch in sorted(current):
                if ch not in indexed:
                    report.missing.append(ch)
                elif indexed[ch] != hash_text(current[ch]):
                    report.drifted.append(ch)
            report.orphaned = sorted(set(indexed) - set(current))
            # 缺陷 #118：正文对账过了不代表图谱完整——entity-deltas.jsonl 损坏时
            # 实体会静默缩水（实测 13 → 4）而上面三项全空。把数据源问题并进 ok。
            try:
                report.entity_source = self.store.read_entity_deltas_report().problem(
                    "实体增量 entity-deltas.jsonl"
                )
            except Exception:
                report.entity_source = ""
            # 缺陷 #127：hash 全对不代表索引是完整的——embedding 挂了（或压根没配）
            # 时向量表是空的，检索悄悄退回 BM25，而上面三项全空、ok=True。
            # 把向量缺失并进 ok，作者和 cron 才不会以为「混合检索」在正常跑。
            try:
                with self._tx(create=False) as conn:
                    vstats = self._stats(conn)
                    report.vector_source = self._vector_source(vstats)
                    vec_breaks = self._vector_breaks_ok(vstats)
            except Exception:
                report.vector_source = ""
                vec_breaks = False
            report.ok = not (
                report.drifted
                or report.missing
                or report.orphaned
                or report.entity_source
                or vec_breaks
            )
            return report
        except Exception as exc:  # noqa: BLE001
            report.ok = False
            report.error = f"{type(exc).__name__}: {exc}"
            return report

    # ---------------------------------------------------------- 读接口（给 search/graph 用）

    def get_chunks(self, chunk_ids: Sequence[str]) -> dict[str, ChunkRow]:
        if not chunk_ids:
            return {}
        try:
            with self._tx(create=False) as conn:
                marks = ",".join("?" * len(chunk_ids))
                rows = conn.execute(
                    f"SELECT chunk_id, ch, offset, text, hash, para_start, para_end, chars "
                    f"FROM chunks WHERE chunk_id IN ({marks})",
                    list(chunk_ids),
                ).fetchall()
            return {str(r["chunk_id"]): _row_to_chunk(r) for r in rows}
        except Exception:
            return {}

    def chunk_count(self) -> int:
        return self.stats().chunks

    def all_chunks(self) -> list[ChunkRow]:
        try:
            with self._tx(create=False) as conn:
                rows = conn.execute(
                    "SELECT chunk_id, ch, offset, text, hash, para_start, para_end, chars "
                    "FROM chunks ORDER BY ch, offset"
                ).fetchall()
            return [_row_to_chunk(r) for r in rows]
        except Exception:
            return []


def _bare_id(raw: str) -> str:
    """``characters/mc.yaml`` → ``mc``；``@mc`` → ``mc``。"""
    s = str(raw or "").strip()
    if not s:
        return ""
    s = s.lstrip("@")
    if s.startswith("characters/"):
        s = s[len("characters/"):]
    if s.endswith((".yaml", ".yml")):
        s = s.rsplit(".", 1)[0]
    if s.startswith("chars/"):
        s = s[len("chars/"):]
    if "/" in s:
        s = s.rsplit("/", 1)[-1]
        if s.endswith((".yaml", ".yml")):
            s = s.rsplit(".", 1)[0]
    return s


def _row_to_chunk(row: Any) -> ChunkRow:
    keys = row.keys() if hasattr(row, "keys") else []
    def g(name: str, default: Any = 0) -> Any:
        return row[name] if name in keys else default
    text = str(g("text", ""))
    return ChunkRow(
        chunk_id=str(g("chunk_id", "")),
        ch=int(g("ch", 0) or 0),
        offset=int(g("offset", 0) or 0),
        text=text,
        hash=str(g("hash", "")),
        para_start=int(g("para_start", 0) or 0),
        para_end=int(g("para_end", 0) or 0),
        chars=int(g("chars", 0) or 0) or count_chars(text),
    )


__all__ = [
    "query_phrases",
    "INDEX_DB",
    "SCHEMA_VERSION",
    "FTS_TOKENIZE",
    "ChunkRow",
    "IndexStats",
    "BuildReport",
    "DriftReport",
    "IndexBuilder",
    "cjk_tokens",
    "tokenize_for_fts",
    "build_match_expr",
    "query_match_expr",
]
