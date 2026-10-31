"""检索子系统对外唯一入口（供 context 装配调用）。

契约（与 ``server/context/assembler.py`` 的 ``_build_l4`` 对齐）
----------------------------------------------------------------
- ``available`` 属性决定上层是否降级；
- ``await search_for_outline(outline, top_k=8, budget_chars=N) -> RetrievalResult``；
- result 上取 ``rendered`` / ``mode`` / ``degraded`` / ``hits``（``hits[i].ch`` 用于
  ``[ch-XXXX]`` 来源列表）。

**铁律：这里绝不向上抛异常。** 任何环节炸了都收敛成 ``mode="off"`` 的空结果，
让装配流程继续跑（§8.2 降级链的最后一级）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from server.core.schema import ChapterOutline
from server.core.store import NovelStore

from .chunker import citation as make_citation
from .embedder import Embedder, get_embedder
from .graph import ExpandedEntity, GraphIndex
from .index import BuildReport, DriftReport, IndexBuilder
from .search import (
    DEFAULT_TOP_K,
    Hit,
    HybridSearch,
    NullReranker,
    Query,
    Reranker,
)

#: L4 默认注入预算（字符）
DEFAULT_BUDGET_CHARS = 6000

#: 从章纲文本里抽关键词时的停用词/噪音
_STOPWORDS = {
    "的", "了", "和", "与", "在", "是", "把", "被", "对", "为", "从", "到", "一个", "然后",
    "自己", "这个", "那个", "他们", "我们", "什么", "因为", "所以", "但是", "而且", "以及",
}


@dataclass
class RetrievalResult:
    """一次 L4 检索的完整产物。"""

    hits: list[Hit] = field(default_factory=list)
    expanded: list[ExpandedEntity] = field(default_factory=list)
    degraded: bool = False
    mode: str = "off"
    rendered: str = ""
    query: str = ""
    error: str = ""
    stats: dict[str, Any] = field(default_factory=dict)

    @property
    def sources(self) -> list[str]:
        """``["[ch-0001]", ...]``，与 ``rendered`` 里的片段一一对应。"""
        return [f"[ch-{h.ch:04d}]" for h in self.hits]

    def __bool__(self) -> bool:
        return bool(self.hits or self.rendered)


class RetrievalFacade:
    """上层唯一入口。构造后所有方法都安全（不会抛）。"""

    def __init__(
        self,
        store: NovelStore,
        embedder: Embedder | None = None,
        reranker: Reranker | None = None,
        enabled: bool = True,
    ):
        self.store = store
        self.enabled = bool(enabled)
        self._explicit_embedder = embedder
        self._explicit_reranker = reranker
        self._index: IndexBuilder | None = None
        self._graph: GraphIndex | None = None
        self._search: HybridSearch | None = None
        self._init_error = ""

    # ---------------------------------------------------------- 内部装配

    def _embedder(self) -> Embedder | None:
        if self._explicit_embedder is not None:
            return self._explicit_embedder
        try:
            return get_embedder(getattr(self.store, "config", None))
        except Exception:
            return None

    def _get_index(self) -> IndexBuilder | None:
        if self._index is None:
            try:
                self._index = IndexBuilder(self.store, embedder=self._embedder())
            except Exception as exc:  # noqa: BLE001
                self._init_error = f"{type(exc).__name__}: {exc}"
                return None
        return self._index

    def _get_graph(self) -> GraphIndex | None:
        if self._graph is None:
            idx = self._get_index()
            if idx is None:
                return None
            try:
                self._graph = GraphIndex(self.store, index=idx)
            except Exception as exc:  # noqa: BLE001
                self._init_error = f"{type(exc).__name__}: {exc}"
                return None
        return self._graph

    def _get_search(self) -> HybridSearch | None:
        if self._search is None:
            idx = self._get_index()
            if idx is None:
                return None
            reranker = self._explicit_reranker if self._explicit_reranker is not None else NullReranker()
            try:
                self._search = HybridSearch(
                    self.store,
                    index=idx,
                    embedder=self._embedder(),
                    reranker=reranker,
                )
            except Exception as exc:  # noqa: BLE001
                self._init_error = f"{type(exc).__name__}: {exc}"
                return None
        return self._search

    # ---------------------------------------------------------- available

    @property
    def available(self) -> bool:
        """索引就绪且子系统启用。**永不抛**。"""
        if not self.enabled:
            return False
        try:
            idx = self._get_index()
            return bool(idx is not None and idx.index_ready())
        except Exception:
            return False

    @property
    def mode(self) -> str:
        """当前能力档位：``hybrid``（有 embedder **且索引里真有向量**）/ ``bm25`` / ``off``。

        缺陷 #127：原来只看「有没有 embedder」，不看「向量表是不是空的」。
        实测 embedding 服务 502 时 build 照常成功（chunks=12/vectors=0），
        这里却报 hybrid —— 门面明明在跑 BM25，档位却写着混合检索。
        """
        try:
            if not self.available:
                return "off"
            if self._embedder() is None:
                return "bm25"
            idx = self._get_index()
            if idx is None:
                return "bm25"
            return "hybrid" if int(idx.stats().vectors or 0) > 0 else "bm25"
        except Exception:
            return "off"

    # ---------------------------------------------------------- 查询构造

    def _query_from_outline(self, outline: ChapterOutline) -> Query:
        """§8.2 查询构造：出场实体名 + 章纲关键词 + must_include 词条。"""
        terms: list[str] = []
        entities: list[str] = []

        try:
            refs = list(outline.char_refs())
        except Exception:
            refs = []
        for ref in refs:
            bare = str(ref).split("/")[-1]
            if bare.endswith((".yaml", ".yml")):
                bare = bare.rsplit(".", 1)[0]
            if bare:
                entities.append(bare)
            # 同时补上角色真名（章纲里可能只写 id）
            try:
                card = self.store.get_character(bare)
            except Exception:
                card = None
            if card is not None:
                if getattr(card, "name", ""):
                    terms.append(str(card.name))
                for alias in getattr(card, "aliases", []) or []:
                    terms.append(str(alias))

        texts: list[str] = []
        if getattr(outline, "title", ""):
            texts.append(str(outline.title))
        texts.extend(str(x) for x in (getattr(outline, "must_include", None) or []))
        for scene in getattr(outline, "scenes", None) or []:
            for attr in ("goal", "conflict", "outcome"):
                val = getattr(scene, attr, "")
                if val:
                    texts.append(str(val))
        for attr in ("beat", "hook", "notes"):
            val = getattr(outline, attr, "")
            if val:
                texts.append(str(val))

        terms.extend(_extract_keywords(texts))
        terms = list(dict.fromkeys(t for t in (str(x).strip() for x in terms) if t and t not in _STOPWORDS))
        entities = list(dict.fromkeys(e for e in entities if e))
        threads = getattr(outline, "threads", None)
        return Query(
            terms=terms,
            entities=entities,
            text=" ".join(texts).strip(),
            threads=list(threads) if threads else None,
        )

    # ---------------------------------------------------------- 主接口

    async def search_for_outline(
        self,
        outline: ChapterOutline,
        top_k: int = DEFAULT_TOP_K,
        budget_chars: int = DEFAULT_BUDGET_CHARS,
    ) -> RetrievalResult:
        """按章纲检索 L4 素材。**任何异常都降级为 ``mode="off"``，不抛。**"""
        try:
            budget = max(200, int(budget_chars or DEFAULT_BUDGET_CHARS))
        except (TypeError, ValueError):
            budget = DEFAULT_BUDGET_CHARS
        try:
            k = max(1, int(top_k or DEFAULT_TOP_K))
        except (TypeError, ValueError):
            k = DEFAULT_TOP_K

        if not self.enabled:
            return RetrievalResult(mode="off", degraded=True, error="disabled")

        try:
            if not self.available:
                return RetrievalResult(mode="off", degraded=True, error="index_not_ready")

            query = self._query_from_outline(outline)
            search = self._get_search()
            if search is None:
                return RetrievalResult(mode="off", degraded=True, error=self._init_error or "search_unavailable")

            exclude_recent = self._recent_chapter(outline)
            try:
                hits = search.search(query, top_k=k, exclude_recent_ch=exclude_recent)
            except Exception as exc:  # noqa: BLE001
                return RetrievalResult(
                    mode="off", degraded=True, error=f"search_failed: {type(exc).__name__}",
                    query=query.flat_text(),
                )

            expanded: list[ExpandedEntity] = []
            if getattr(outline, "__dict__", None) is not None:
                try:
                    expanded = self.graph_expand_local(outline.char_refs())
                except Exception:
                    expanded = []

            mode = "hybrid" if (self._embedder() is not None and any(h.vec_score for h in hits)) else "bm25"
            degraded = mode != "hybrid"
            rendered = self._render(hits, expanded, budget)
            return RetrievalResult(
                hits=hits,
                expanded=expanded,
                degraded=degraded,
                mode=mode,
                rendered=rendered,
                query=query.flat_text(),
                stats={"hits": len(hits), "expanded": len(expanded), "budget_chars": budget},
            )
        except Exception as exc:  # noqa: BLE001 - 装配绝不能崩
            return RetrievalResult(mode="off", degraded=True, error=f"{type(exc).__name__}: {exc}")

    async def graph_expand(self, char_ids: list[str]) -> list[ExpandedEntity]:
        """一跳图谱扩展。**不抛**：图谱不可用返回空列表。"""
        try:
            graph = self._get_graph()
            if graph is None or not self.available:
                return []
            return graph.expand_context(list(char_ids or []), budget_chars=1200)
        except Exception:
            return []

    def graph_expand_local(
        self,
        char_ids: Iterable[str],
        budget_chars: int = 1200,
    ) -> list[ExpandedEntity]:
        """同步版一跳扩展（内部用）。"""
        try:
            graph = self._get_graph()
            if graph is None or not self.available:
                return []
            return graph.expand_context(list(char_ids or []), budget_chars=budget_chars)
        except Exception:
            return []

    # ---------------------------------------------------------- 运维

    def build_index(self, full: bool = False) -> dict:
        """建/更新索引。返回报告 dict（失败带 ``error``，不抛）。"""
        if not self.enabled:
            return {"error": "disabled", "changed": [], "removed": [], "vector_source": ""}
        try:
            idx = self._get_index()
            if idx is None:
                return {
                    "error": self._init_error or "index_unavailable",
                    "changed": [],
                    "removed": [],
                    "vector_source": "",
                }
            report: BuildReport = idx.build(full=bool(full))
            # 建完丢掉缓存的读句柄，确保下次读到新数据
            self._graph = None
            self._search = None
            return report.as_dict()
        except Exception as exc:  # noqa: BLE001
            return {
                "error": f"{type(exc).__name__}: {exc}",
                "changed": [],
                "removed": [],
                "vector_source": "",
            }

    def verify(self) -> dict:
        """hash 对账（§8.5 夜间校验）。返回 dict；``ok=False`` 表示有漂移或库缺失。"""
        if not self.enabled:
            return {
                "ok": False,
                "error": "disabled",
                "drifted": [],
                "missing": [],
                "orphaned": [],
                "entity_source": "",
                "vector_source": "",
            }
        try:
            idx = self._get_index()
            if idx is None:
                return {
                    "ok": False,
                    "error": self._init_error or "index_unavailable",
                    "drifted": [],
                    "missing": [],
                    "orphaned": [],
                    "entity_source": "",
                    "vector_source": "",
                }
            report: DriftReport = idx.verify()
            return report.as_dict()
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
                "drifted": [],
                "missing": [],
                "orphaned": [],
                "entity_source": "",
                "vector_source": "",
            }

    def stats(self) -> dict:
        try:
            idx = self._get_index()
            if idx is None:
                return {"ready": False, "error": self._init_error or "index_unavailable"}
            out = idx.stats().as_dict()
            out["ready"] = idx.index_ready()
            out["mode"] = self.mode
            return out
        except Exception as exc:  # noqa: BLE001
            return {"ready": False, "error": f"{type(exc).__name__}: {exc}"}

    # ---------------------------------------------------------- 渲染

    def _recent_chapter(self, outline: ChapterOutline) -> int | None:
        """本章章号 - 1 = 最近 1 章（§8.3：排除最近 1 章原文，L3 已有全文尾部）。"""
        try:
            ch = int(getattr(outline, "ch", 0) or 0)
        except (TypeError, ValueError):
            return None
        return (ch - 1) if ch > 1 else None

    def _render(
        self,
        hits: Sequence[Hit],
        expanded: Sequence[ExpandedEntity],
        budget_chars: int,
    ) -> str:
        """拼可注入文本：每段以 ``[ch-XXXX:段N]`` 开头，总长受 ``budget_chars`` 约束。"""
        blocks: list[str] = []
        used = 0
        for hit in hits:
            cite = hit.citation or make_citation(hit.ch, hit.para_start, hit.para_end)
            body = (hit.text or "").strip()
            if not body:
                continue
            block = f"{cite} {body}"
            if used + len(block) > budget_chars:
                remain = budget_chars - used
                if remain <= len(cite) + 16:
                    break
                block = block[:remain]
            blocks.append(block)
            used += len(block) + 1
            if used >= budget_chars:
                break
        return "\n\n".join(blocks)


# ------------------------------------------------------------------ 工具


_KW_RE = re.compile(r"[\u4e00-\u9fff]{2,6}|[A-Za-z]{3,}")


def _extract_keywords(texts: Iterable[str], limit: int = 40) -> list[str]:
    """从章纲文本里抽候选关键词（中文 2-6 字切片 + 英文词）。

    刻意保守：只做「非停用词」过滤，不做词性标注（没有分词依赖）。
    """
    out: list[str] = []
    for text in texts:
        for m in _KW_RE.finditer(str(text or "")):
            tok = m.group(0)
            if tok in _STOPWORDS or len(tok) < 2:
                continue
            out.append(tok)
            if len(out) >= limit:
                return out
    return out


def build_facade(
    store: NovelStore,
    config: Any = None,
    *,
    enabled: bool = True,
) -> RetrievalFacade:
    """按配置构造 facade（embedder/reranker 都从 config 里取，取不到就降级）。"""
    embedder = None
    reranker = None
    try:
        embedder = get_embedder(config)
    except Exception:
        embedder = None
    try:
        from .search import get_reranker

        reranker = get_reranker(config)
    except Exception:
        reranker = None
    return RetrievalFacade(store, embedder=embedder, reranker=reranker, enabled=enabled)


__all__ = [
    "RetrievalResult",
    "RetrievalFacade",
    "build_facade",
    "DEFAULT_BUDGET_CHARS",
]
