"""检索子系统（§8 RAG / §8.4 GraphRAG / §8.5 索引更新）。

对外只需要 ``RetrievalFacade``（``facade.py``），其余模块是它的零件：

- ``chunker``  —— 中文段落对齐分块
- ``index``    —— SQLite + FTS5 索引（可随时重建的缓存）
- ``embedder`` —— 可插拔 embedding（没配置就返回 ``None``，检索降级到纯 BM25）
- ``search``   —— 双通道 + RRF + 重排
- ``graph``    —— GraphRAG 实体图谱
- ``facade``   —— 上层唯一入口，所有异常吞掉降级为 ``mode="off"``
"""

from __future__ import annotations

from .chunker import Chunk, chunk_chapter, chunk_stats, citation, hash_text
from .embedder import (
    Embedder,
    HashEmbedder,
    OpenAICompatEmbedder,
    get_embedder,
)
from .facade import (
    DEFAULT_BUDGET_CHARS,
    RetrievalFacade,
    RetrievalResult,
    build_facade,
)
from .graph import (
    REL_CANON,
    Edge,
    Entity,
    ExpandedEntity,
    GraphIndex,
    canon_rel,
)
from .index import (
    SCHEMA_VERSION,
    BuildReport,
    ChunkRow,
    DriftReport,
    IndexBuilder,
    IndexStats,
    cjk_tokens,
    query_match_expr,
    tokenize_for_fts,
)
from .search import (
    DEFAULT_TOP_K,
    RRF_K,
    Hit,
    HybridSearch,
    NullReranker,
    OpenAICompatReranker,
    Query,
    Reranker,
    get_reranker,
)

__all__ = [
    # chunker
    "Chunk",
    "chunk_chapter",
    "chunk_stats",
    "citation",
    "hash_text",
    # embedder
    "Embedder",
    "HashEmbedder",
    "OpenAICompatEmbedder",
    "get_embedder",
    # index
    "SCHEMA_VERSION",
    "BuildReport",
    "ChunkRow",
    "DriftReport",
    "IndexBuilder",
    "IndexStats",
    "cjk_tokens",
    "query_match_expr",
    "tokenize_for_fts",
    # search
    "DEFAULT_TOP_K",
    "RRF_K",
    "Hit",
    "HybridSearch",
    "NullReranker",
    "OpenAICompatReranker",
    "Query",
    "Reranker",
    "get_reranker",
    # graph
    "REL_CANON",
    "Edge",
    "Entity",
    "ExpandedEntity",
    "GraphIndex",
    "canon_rel",
    # facade
    "DEFAULT_BUDGET_CHARS",
    "RetrievalFacade",
    "RetrievalResult",
    "build_facade",
]
