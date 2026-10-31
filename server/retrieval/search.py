"""混合检索管线（§8.2 + §8.3）。

严格按文档顺序::

    Query(terms + entities + text)
      ↓ 查询构造（实体名 + 关键词 + must_include 词条 → FTS5 MATCH 表达式）
      ↓ 双通道：BM25(FTS5) top-20  ‖  向量 top-20
      ↓ RRF 融合(k=60) → top-20
      ↓ 重排（可选 reranker） → top-k（默认 8）
      ↓ 出处标注 Hit.citation = "[ch-0042:段3]"

降级链（§8.2）
--------------
- 有 embedder 且有向量 → ``mode="hybrid"``，双通道 + RRF；
- 无 embedder / 向量缺失 / embedding 调用失败 → ``mode="bm25"``，只用 FTS5；
- 索引未就绪 → 上层 ``RetrievalFacade`` 直接 ``mode="off"``。

**任何一路失败都不中断**：向量挂了退回 BM25，rerank 挂了保留 RRF 顺序。
"""
from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

import numpy as np

from server.core.store import NovelStore

from .chunker import citation as make_citation
from .embedder import Embedder, cosine_topk, normalize_vec, run_coro_sync
from .index import ChunkRow, IndexBuilder, query_match_expr, query_phrases

#: RRF 平滑常数（§8.2：k=60）
RRF_K = 60
#: 每路召回的候选数
CHANNEL_TOP = 20
#: 融合后进入重排的候选数
FUSED_TOP = 20
#: 默认最终返回条数
DEFAULT_TOP_K = 8


# ================================================================== 数据结构


@dataclass
class Query:
    """检索查询三要素（§8.2 查询构造）。"""

    terms: list[str] = field(default_factory=list)
    entities: list[str] = field(default_factory=list)
    text: str = ""
    threads: list[str] | None = None

    def keywords(self) -> list[str]:
        return [t for t in self.terms if str(t).strip()]

    def is_empty(self) -> bool:
        return not (self.text.strip() or self.keywords() or [e for e in self.entities if str(e).strip()])

    def flat_text(self) -> str:
        parts = [self.text.strip()]
        parts.extend(str(t).strip() for t in self.terms if str(t).strip())
        parts.extend(str(e).strip() for e in self.entities if str(e).strip())
        return " ".join(p for p in parts if p)


@dataclass
class Hit:
    """一条检索结果。``citation`` 是 M4 验收要求的出处标注。"""

    chunk_id: str
    ch: int
    text: str
    score: float = 0.0
    bm25_score: float = 0.0
    vec_score: float = 0.0
    rrf_score: float = 0.0
    rerank_score: float | None = None
    citation: str = ""
    para_start: int = 0
    para_end: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "ch": self.ch,
            "text": self.text,
            "score": self.score,
            "bm25_score": self.bm25_score,
            "vec_score": self.vec_score,
            "rrf_score": self.rrf_score,
            "rerank_score": self.rerank_score,
            "citation": self.citation,
        }


# ================================================================== 重排


class Reranker:
    """重排接口。输入 ``(query, 候选文本)``，返回每个候选的相关性分数。"""

    name = "reranker"

    async def rerank(self, query: str, texts: list[str]) -> list[float]:
        raise NotImplementedError

    def rerank_sync(self, query: str, texts: list[str]) -> list[float] | None:
        try:
            scores = run_coro_sync(self.rerank(query, list(texts)))
        except Exception:
            return None
        if len(scores) != len(texts):
            return None
        return [float(s) for s in scores]


class NullReranker(Reranker):
    """空重排：不改顺序（分数恒为 0，调用方按原序保留）。"""

    name = "null"

    async def rerank(self, query: str, texts: list[str]) -> list[float]:
        return [0.0] * len(texts)


class OpenAICompatReranker(Reranker):
    """OpenAI 兼容 ``POST {base_url}/v1/rerank``（bge-reranker / TEI / cohere 风格）。"""

    name = "openai_compat"

    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        model: str = "bge-reranker-v2-m3",
        *,
        timeout: float = 30.0,
        extra_headers: Mapping[str, str] | None = None,
    ):
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.model = model or "bge-reranker-v2-m3"
        self.timeout = float(timeout)
        self.extra_headers = dict(extra_headers or {})

    @property
    def url(self) -> str:
        from .embedder import _join_url

        return _join_url(self.base_url, "/v1/rerank")

    def _headers(self) -> dict[str, str]:
        headers = {"content-type": "application/json"}
        if self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"
        headers.update(self.extra_headers)
        return headers

    def payload(self, query: str, texts: list[str]) -> dict[str, Any]:
        return {"model": self.model, "query": query, "documents": list(texts), "top_n": len(texts)}

    async def rerank(self, query: str, texts: list[str]) -> list[float]:
        import httpx

        if not texts:
            return []
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(self.url, headers=self._headers(), json=self.payload(query, texts))
            resp.raise_for_status()
            data = resp.json()
        scores = [0.0] * len(texts)
        results = data.get("results") or data.get("data") or []
        for item in results:
            idx = item.get("index", item.get("document_index"))
            if isinstance(idx, int) and 0 <= idx < len(scores):
                scores[idx] = float(item.get("relevance_score", item.get("score", 0.0)) or 0.0)
            elif "document" in item:
                try:
                    scores[texts.index(item["document"])] = float(
                        item.get("relevance_score", item.get("score", 0.0)) or 0.0
                    )
                except ValueError:
                    pass
        return scores


def get_reranker(config: Any = None) -> Reranker | None:
    """按配置构造 reranker。没配 → ``None``（用 ``NullReranker``）；**永不抛**。"""
    try:
        settings: dict[str, Any] = {}
        if isinstance(config, Mapping):
            sub = config.get("reranker") or config.get("rerank")
            if isinstance(sub, Mapping):
                settings = dict(sub)
            elif any(k in config for k in ("provider", "model", "base_url")):
                settings = dict(config)
        elif config is not None:
            sub = getattr(config, "reranker", None)
            if sub is not None:
                settings = dict(sub) if isinstance(sub, Mapping) else {}
        if not settings or settings.get("enabled") is False:
            return None
        provider = str(settings.get("provider") or "").strip().lower()
        if provider in ("", "none", "off", "null"):
            return None
        base_url = str(settings.get("base_url") or "").strip()
        model = str(settings.get("model") or "").strip()
        if not base_url or not model:
            return None
        key = str(settings.get("api_key") or "").strip()
        if not key:
            import os

            env_name = str(settings.get("api_key_env") or "").strip()
            key = str(os.environ.get(env_name) or "").strip() if env_name else ""
        if provider in ("openai_compat", "openai", "bge", "tei", "cohere") and not key:
            # 本地 TEI 常常无鉴权，但 OpenAI 兼容端点必须有 key —— 保守起见给空串也允许，
            # 只排除明显是远端却没 key 的情况。
            if "127.0.0.1" not in base_url and "localhost" not in base_url:
                return None
        return OpenAICompatReranker(
            base_url=base_url,
            api_key=key,
            model=model,
            timeout=float(settings.get("timeout") or 30.0),
        )
    except Exception:
        return None


# ================================================================== 检索器


class HybridSearch:
    """双通道 + RRF + 重排。"""

    def __init__(
        self,
        store: NovelStore,
        index: IndexBuilder | None = None,
        embedder: Embedder | None = None,
        reranker: Reranker | None = None,
    ):
        self.store = store
        self.index = index or IndexBuilder(store, embedder=embedder)
        self.embedder = embedder if embedder is not None else getattr(self.index, "embedder", None)
        self.reranker = reranker

    # ---------------------------------------------------------- 对外

    @property
    def mode(self) -> str:
        return "hybrid" if self.embedder is not None else "bm25"

    def search(
        self,
        query: Query,
        top_k: int = DEFAULT_TOP_K,
        exclude_recent_ch: int | None = None,
    ) -> list[Hit]:
        """执行检索。**不抛异常**：任何失败都退化成空列表或纯 BM25 结果。"""
        if top_k <= 0:
            return []
        try:
            if not isinstance(query, Query):
                query = Query(text=str(query or ""))
            if query.is_empty():
                return []
            excluded = self._excluded_chapters(exclude_recent_ch)
            fts_hits = self._bm25_channel(query, excluded)
            vec_hits = self._vec_channel(query, excluded)
            fused = self._rrf(fuse_sources=[fts_hits, vec_hits])
            if not fused:
                return []
            reranked = self._apply_rerank(query, fused)
            return reranked[:top_k]
        except Exception:
            return []

    # ---------------------------------------------------------- 通道

    def _excluded_chapters(self, exclude_recent_ch: int | None) -> set[int]:
        """§8.3：排除最近 1 章原文（L3 已有全文尾部）。"""
        out: set[int] = set()
        if exclude_recent_ch is None:
            return out
        try:
            n = int(exclude_recent_ch)
        except (TypeError, ValueError):
            return out
        if n > 0:
            out.add(n)
        return out

    def _threads_filter(self, query: Query, rows: list[tuple[ChunkRow, float]]) -> list[tuple[ChunkRow, float]]:
        """§8.3 可选的 threads 过滤。

        索引里没有段落级的线程标记（线程是章纲级语义），所以这里**只做宽松过滤**：
        若某 chunk 文本完全不含任何线程名/关键词，则保留（不误杀）。真正的
        线程级裁剪留给上层 context 装配。保留该钩子便于以后接段级标注。
        """
        return rows

    def _strict_phrases(self, query: Query) -> list[str]:
        """查询里的双字及以上 token，用于精度过滤（单字只用于召回）。"""
        texts: list[str] = [query.text, *query.keywords(), *[str(e) for e in query.entities if str(e).strip()]]
        out: list[str] = []
        for t in texts:
            out.extend(query_phrases(str(t)))
        return list(dict.fromkeys(x for x in out if x))

    def _bm25_channel(self, query: Query, excluded: set[int]) -> list[tuple[str, float]]:
        """FTS5 BM25 通道。返回 ``[(chunk_id, bm25_raw)]``，raw 越小越相关。"""
        expr = self._match_expr(query)
        if not expr:
            return []
        try:
            conn = self.index.connect(create=False)
        except Exception:
            return []
        try:
            # 注意：FTS5 的 MATCH 必须用表名，不能用别名
            sql = (
                "SELECT chunk_id, ch, bm25(chunks_fts) AS score FROM chunks_fts "
                "WHERE chunks_fts MATCH ? ORDER BY score LIMIT ?"
            )
            try:
                rows = conn.execute(sql, (expr, CHANNEL_TOP * 3)).fetchall()
            except sqlite3.OperationalError:
                return []
            strict = self._strict_phrases(query)
            candidates = [str(r["chunk_id"]) for r in rows if int(r["ch"]) not in excluded]
            bodies = self.index.get_chunks(candidates)
            out: list[tuple[str, float]] = []
            for r in rows:
                cid = str(r["chunk_id"])
                if int(r["ch"]) in excluded:
                    continue
                if strict:
                    # 精度过滤：单字只负责召回，命中结果里必须出现至少一个双字词
                    row = bodies.get(cid)
                    if row is None or not any(p in row.text for p in strict):
                        continue
                out.append((cid, float(r["score"])))
                if len(out) >= CHANNEL_TOP:
                    break
            return out
        finally:
            conn.close()

    def _match_expr(self, query: Query) -> str:
        """查询构造：text 大 OR 组 + 每个 term/entity 各自成组（组间 OR 放宽召回）。"""
        expr = query_match_expr(
            text=query.text,
            terms=query.keywords(),
            entities=[e for e in query.entities if str(e).strip()],
            mode="any",
        )
        return expr

    def _vec_channel(self, query: Query, excluded: set[int]) -> list[tuple[str, float]]:
        """向量通道。返回 ``[(chunk_id, cosine)]``，已按分数降序。"""
        if self.embedder is None:
            return []
        qtext = query.flat_text()
        if not qtext.strip():
            return []
        try:
            qvec = run_coro_sync(self.embedder.embed([qtext]))[0]
        except Exception:
            return []
        if not qvec:
            return []
        try:
            conn = self.index.connect(create=False)
        except Exception:
            return []
        try:
            rows = conn.execute(
                "SELECT v.chunk_id AS chunk_id, c.ch AS ch, v.vec AS vec "
                "FROM vectors v JOIN chunks c ON c.chunk_id = v.chunk_id"
            ).fetchall()
        finally:
            conn.close()
        if not rows:
            return []

        ids: list[str] = []
        chs: list[int] = []
        mats: list[np.ndarray] = []
        for r in rows:
            try:
                mats.append(np.frombuffer(r["vec"], dtype=np.float32))
                ids.append(str(r["chunk_id"]))
                chs.append(int(r["ch"]))
            except Exception:
                continue
        if not mats:
            return []
        dim = mats[0].shape[0]
        mats = [m for m in mats if m.shape[0] == dim]
        if not mats:
            return []
        qv = normalize_vec(qvec[:dim] if len(qvec) >= dim else qvec)
        if qv.shape[0] != dim:
            return []
        matrix = np.vstack(mats)
        # 先多取一些，剔掉被排除的章后再截断
        pairs = cosine_topk(qv, matrix, min(len(ids), CHANNEL_TOP * 3))
        out: list[tuple[str, float]] = []
        for idx, score in pairs:
            if idx >= len(ids):
                continue
            if chs[idx] in excluded:
                continue
            out.append((ids[idx], float(score)))
            if len(out) >= CHANNEL_TOP:
                break
        return out

    # ---------------------------------------------------------- 融合

    @staticmethod
    def rrf_fuse(
        ranked_lists: Sequence[Sequence[str]],
        k: int = RRF_K,
    ) -> list[tuple[str, float]]:
        """RRF：``score(d) = Σ 1/(k + rank_i(d))``，rank 从 1 起。"""
        scores: dict[str, float] = {}
        for ranked in ranked_lists:
            for rank, doc in enumerate(ranked, start=1):
                if not doc:
                    continue
                scores[doc] = scores.get(doc, 0.0) + 1.0 / (k + rank)
        return sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))

    def _rrf(
        self,
        fuse_sources: Sequence[Sequence[tuple[str, float]]],
        limit: int = FUSED_TOP,
    ) -> list[Hit]:
        ranked_lists = [[cid for cid, _ in src] for src in fuse_sources]
        fused = self.rrf_fuse(ranked_lists)
        if not fused:
            return []
        fused = fused[:limit]
        bm25_map = {cid: s for cid, s in (fuse_sources[0] if len(fuse_sources) > 0 else [])}
        vec_map = {cid: s for cid, s in (fuse_sources[1] if len(fuse_sources) > 1 else [])}
        rows = self.index.get_chunks([cid for cid, _ in fused])
        hits: list[Hit] = []
        for cid, rrf_score in fused:
            row = rows.get(cid)
            if row is None:
                continue
            hits.append(
                Hit(
                    chunk_id=cid,
                    ch=row.ch,
                    text=row.text,
                    score=rrf_score,
                    bm25_score=bm25_map.get(cid, 0.0),
                    vec_score=vec_map.get(cid, 0.0),
                    rrf_score=rrf_score,
                    citation=make_citation(row.ch, row.para_start, row.para_end),
                    para_start=row.para_start,
                    para_end=row.para_end,
                )
            )
        return hits

    # ---------------------------------------------------------- 重排

    def _apply_rerank(self, query: Query, hits: list[Hit]) -> list[Hit]:
        """可选重排。reranker 为 None / 失败 / 分数长度不符 → 保留 RRF 顺序。"""
        reranker = self.reranker
        if reranker is None or not hits:
            return hits
        if isinstance(reranker, NullReranker):
            return hits
        scores = reranker.rerank_sync(query.flat_text(), [h.text for h in hits])
        if scores is None:
            return hits
        for hit, score in zip(hits, scores):
            hit.rerank_score = float(score)
            # 重排分数是主序，RRF 作次级（保留兜底可解释性）
            hit.score = float(score)
        return sorted(hits, key=lambda h: (-(h.rerank_score or 0.0), -h.rrf_score))

    # ---------------------------------------------------------- 调试

    def explain(self, query: Query, top_k: int = DEFAULT_TOP_K) -> dict[str, Any]:
        """返回两通道原始候选 + 融合结果，便于排查召回问题（测试/调试用）。"""
        excluded: set[int] = set()
        bm25 = self._bm25_channel(query, excluded)
        vec = self._vec_channel(query, excluded)
        fused = self.rrf_fuse([[c for c, _ in bm25], [c for c, _ in vec]])
        return {
            "match_expr": self._match_expr(query),
            "mode": self.mode,
            "bm25": bm25[:top_k],
            "vector": vec[:top_k],
            "fused": fused[:top_k],
        }


__all__ = [
    "Query",
    "Hit",
    "Reranker",
    "NullReranker",
    "OpenAICompatReranker",
    "get_reranker",
    "HybridSearch",
    "RRF_K",
    "CHANNEL_TOP",
    "FUSED_TOP",
    "DEFAULT_TOP_K",
    "math",
]
