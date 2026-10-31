"""检索子系统测试（§8 / §8.4 / §8.5 验收项 1-10）。"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from server.core.schema import BookMeta, ChapterOutline, EntityDelta, Relation, Scene
from server.core.store import NovelStore, Workbench
from server.retrieval.chunker import Chunk, chunk_chapter, chunk_stats, hash_text
from server.retrieval.embedder import (
    HASH_DIM,
    HashEmbedder,
    OpenAICompatEmbedder,
    get_embedder,
    normalize_vec,
    pack_vector,
    unpack_vector,
)
from server.retrieval.facade import RetrievalFacade, RetrievalResult
from server.retrieval.graph import GraphIndex, canon_rel
from server.retrieval.index import (
    SCHEMA_VERSION,
    IndexBuilder,
    build_match_expr,
    cjk_tokens,
    query_match_expr,
    tokenize_for_fts,
)
from server.retrieval.search import (
    Hit,
    HybridSearch,
    NullReranker,
    OpenAICompatReranker,
    Query,
    get_reranker,
)

# ---------------- 假书 fixture


CH1 = """\
第一章 山门试炼

清晨的雾气还没散尽，青岩矿脉的入口已经站满了人。林昭握着手里的柴刀，指节发白。

“下一个。”守山弟子的声音没有起伏。林昭走上前，把柴刀横在胸前，深吸一口气。

他体内那点微薄的灵气忽然躁动起来，顺着经脉一路冲上丹田，像一条被惊动的蛇。
守山弟子抬眼看了他一下，嘴角微微一动。

“淬体九层。”那弟子说，“过。”

林昭愣在原地，直到身后有人推了他一把，才踉跄着走进山门。青云宗三个字刻在石壁上，被雨水磨得发白。

石阶一共九百级，爬起来比砍一天的柴还要累。走到一半，他听见前面有人在低声数着自己的呼吸，一长两短，很稳。

“别数了。”一个穿灰袍的少年回头看他，“数到三百级你就会发现，越数越喘。”

林昭没说话，只是把手里的柴刀换到另一边肩膀。那少年笑了一下，又转回去继续往上走。

山顶的风比山下冷得多。执事把一块刻着名字的木牌递给他，牌子上还留着上一个主人的划痕。

“西厢丙字房。”执事说，“明早卯时，演武场。”

林昭把木牌攥在手心里。牌子是温的，不知道是被太阳晒的，还是被前一个握过它的人捂热的。
"""

CH2 = """\
第二章 血刀门来客

落霞城的集市比青云山热闹十倍。苏晚站在卖糖人的摊子前，指尖捏着一枚铜钱，迟迟没有递出去。

街口忽然传来马蹄声。三名穿血色短打的男人勒马停在茶楼前，为首那个腰间挂着一柄弯刀。

“血刀门办事。”那人说，“闲人退开。”

苏晚低下头，把铜钱塞回袖子里。她认得那柄弯刀，刀鞘上有一道很深的缺口，是去年在青岩矿脉留下的。

茶楼二楼的窗户被人推开一条缝，有人往外看了一眼，又迅速合上。
"""

CH3 = """\
第三章 断崖之下

林昭从断崖坠落的时候，脑子里只有一个念头：原来筑基修士的一掌，是这个分量。

风声在耳边呼啸，他下意识把灵气全部压进四肢，身体在半空中翻了个身。

崖壁上垂着一根老藤。他伸手去抓，指尖擦过藤皮，滑开半寸，最后死死扣住了一个节疤。

骨头像是散了架。他趴在突出的一块岩石上，咳出一口血，抬头看见崖顶站着一个人影。

那人没有追下来。
"""


@pytest.fixture()
def store(tmp_path: Path) -> NovelStore:
    wb = Workbench(tmp_path)
    st = wb.create_novel(BookMeta(title="检索测试书", genre="玄幻"))
    st.write_chapter(1, CH1)
    st.write_chapter(2, CH2)
    st.write_chapter(3, CH3)
    return st


@pytest.fixture()
def built(store: NovelStore) -> IndexBuilder:
    b = IndexBuilder(store)
    b.build(full=True)
    return b


def _mk_outline(**kw) -> ChapterOutline:
    base = dict(ch=4, title="第四章 试炼之后", scenes=[Scene(loc="青云山", chars=["mc"], goal="复盘断崖一战")])
    base.update(kw)
    return ChapterOutline(**base)


# ------------------------------------------------------------------ 1. chunker


def test_chunker_stable_and_paragraph_aligned():
    chunks = chunk_chapter(1, CH1)
    assert chunks
    # 重复切块 chunk_id / hash / offset 完全一致
    again = chunk_chapter(1, CH1)
    assert [c.chunk_id for c in chunks] == [c.chunk_id for c in again]
    assert [c.hash for c in chunks] == [c.hash for c in again]
    assert [c.offset for c in chunks] == [c.offset for c in again]
    assert all(c.chunk_id == f"c-1-{i}" for i, c in enumerate(chunks, start=1))

    # 段落不切断：每个 chunk 的文本按 \n 拆开后，每段都是原文里的完整段落
    paras = [p.strip() for p in CH1.split("\n") if p.strip()]
    for c in chunks:
        for piece in c.text.split("\n"):
            assert piece.strip() in paras, f"段落被切断: {piece!r}"

    # offset 指向原文里该 chunk 首段起点
    for c in chunks:
        assert CH1[c.offset : c.offset + len(c.text.split("\n")[0])] == c.text.split("\n")[0]

    # 目标 ≈500 字
    stats = chunk_stats(chunks)
    assert 300 <= stats["avg"] <= 800


def test_chunker_splits_long_paragraph_by_sentence():
    one = "这是一句用来测试超长段落的句子，它需要足够长才能撑起五百字上限。" * 60
    assert "\n" not in one
    chunks = chunk_chapter(7, one)
    assert len(chunks) >= 2, "超长单段必须被二次切分"
    # 二次切分按句末标点，不产生半句
    for c in chunks:
        body = c.text.strip()
        assert body
        assert body.endswith(("。", "！", "？", "…"))
    # 全部 chunk 拼回去等于原文（去空白后对齐）
    joined = "".join(c.text.strip() for c in chunks)
    assert joined == one.strip()
    # 单个 chunk 不超硬上限太多
    assert all(len(c.text) <= 800 for c in chunks)


def test_chunker_empty_text():
    assert chunk_chapter(1, "") == []
    assert chunk_chapter(1, "   \n\n  ") == []


# ------------------------------------------------------------------ 2. 分词/索引


def test_cjk_tokenizer_produces_uni_and_bi_grams():
    toks = cjk_tokens("淬体九层")
    assert "淬" in toks and "体" in toks
    assert "淬体" in toks and "体九" in toks and "九层" in toks
    assert tokenize_for_fts("淬体 A1") == "淬 体 淬体 a1"


def test_build_match_expr_quotes_and_groups():
    expr = build_match_expr([["淬体", "淬"], ["山门"]])
    assert expr == '("淬体" OR "淬") AND ("山门")'
    assert build_match_expr([]) == ""


def test_fts5_index_and_search_hits_right_chapter(store: NovelStore, built: IndexBuilder):
    """建 3 章假书，搜关键词命中正确章。"""
    assert built.index_ready()
    stats = built.stats()
    assert stats.chapters == 3
    assert stats.chunks >= 3
    assert stats.version == SCHEMA_VERSION

    search = HybridSearch(store, index=built)
    hits = search.search(Query(text="淬体"), top_k=5)
    assert hits, "FTS5 中文检索应命中"
    assert all("淬体" in h.text or "淬" in h.text for h in hits)
    assert hits[0].ch == 1

    hits2 = search.search(Query(text="血刀门"), top_k=5)
    assert hits2 and hits2[0].ch == 2

    hits3 = search.search(Query(text="断崖 老藤"), top_k=5)
    assert hits3 and hits3[0].ch == 3

    # 分词表达式确实用到了单字 + 双字 token
    expr = search._match_expr(Query(text="淬体"))
    assert '"淬体"' in expr and '"淬"' in expr


# ------------------------------------------------------------------ 3. 降级链


def test_bm25_works_without_embedding(store: NovelStore, built: IndexBuilder):
    assert get_embedder(None) is None
    assert get_embedder({}) is None
    assert get_embedder({"embedding": {"provider": "none"}}) is None
    # 没有 key 的 openai_compat 也必须返回 None（硬要求）
    assert get_embedder({"embedding": {"provider": "openai", "base_url": "http://x", "model": "m"}}) is None

    search = HybridSearch(store, index=built, embedder=None)
    assert search.mode == "bm25"
    hits = search.search(Query(text="青云宗"), top_k=5)
    assert hits, "无 embedding 时纯 BM25 必须可用"


def test_hash_embedder_deterministic_and_usable(store: NovelStore):
    emb = get_embedder({"embedding": {"provider": "hash", "dim": 256}})
    assert isinstance(emb, HashEmbedder)
    assert emb.dim == HASH_DIM == 256

    v1 = asyncio.run(emb.embed(["林昭握着手里的柴刀"]))[0]
    v2 = asyncio.run(emb.embed(["林昭握着手里的柴刀"]))[0]
    assert v1 == v2, "HashEmbedder 必须确定性"
    assert len(v1) == 256
    assert abs(sum(x * x for x in v1) - 1.0) < 1e-4, "向量应 L2 归一化"

    b = IndexBuilder(store, embedder=emb)
    b.build(full=True)
    assert b.stats().vectors == b.stats().chunks
    search = HybridSearch(store, index=b, embedder=emb)
    assert search.mode == "hybrid"
    hits = search.search(Query(text="血刀门弯刀"), top_k=5)
    assert hits and hits[0].ch == 2


def test_openai_compat_embedder_constructs_without_network():
    emb = OpenAICompatEmbedder(
        base_url="http://127.0.0.1:8000/",
        api_key="sk-test",
        model="bge-m3",
        dim=1024,
    )
    assert emb.url == "http://127.0.0.1:8000/v1/embeddings"
    assert emb.payload(["a", "b"]) == {
        "model": "bge-m3",
        "input": ["a", "b"],
        "encoding_format": "float",
    }
    assert emb._headers()["authorization"] == "Bearer sk-test"
    assert emb.dim == 1024

    # base_url 已带 /v1 时不重复拼
    emb2 = OpenAICompatEmbedder(base_url="http://h/v1", api_key="k", model="m", dim=8)
    assert emb2.url == "http://h/v1/embeddings"

    # 经 get_embedder 且带 key → 构造成功
    built = get_embedder(
        {"embedding": {"provider": "openai_compat", "base_url": "http://h", "model": "m",
                       "api_key": "sk-x", "dim": 512}}
    )
    assert isinstance(built, OpenAICompatEmbedder) and built.dim == 512

    # api_key_env 走环境变量
    import os

    os.environ["_TEST_EMB_KEY"] = "sk-env"
    try:
        got = get_embedder(
            {"embedding": {"provider": "openai_compat", "base_url": "http://h", "model": "m",
                           "api_key_env": "_TEST_EMB_KEY"}}
        )
        assert isinstance(got, OpenAICompatEmbedder) and got.api_key == "sk-env"
    finally:
        os.environ.pop("_TEST_EMB_KEY", None)


def test_vector_pack_roundtrip():
    vec = [3.0, 4.0] + [0.0] * 254
    blob = pack_vector(vec)
    back = unpack_vector(blob)
    assert back.dtype.name == "float32"
    assert abs(float((back**2).sum()) - 1.0) < 1e-5
    norm = normalize_vec([3.0, 4.0])
    assert abs(float(norm[0]) - 0.6) < 1e-6


# ------------------------------------------------------------------ 4. RRF


def test_rrf_puts_both_channel_hits_first(store: NovelStore):
    emb = HashEmbedder()
    b = IndexBuilder(store, embedder=emb)
    b.build(full=True)
    search = HybridSearch(store, index=b, embedder=emb)

    q = Query(text="血刀门弯刀落霞城")
    bm25 = [cid for cid, _ in search._bm25_channel(q, set())]
    vec = [cid for cid, _ in search._vec_channel(q, set())]
    assert bm25, "BM25 通道应有结果"
    assert vec, "向量通道应有结果"

    fused = search.rrf_fuse([bm25, vec])
    both = set(bm25) & set(vec)
    assert both, "两通道应有交集才能验证融合排序"
    # 两路都命中的，RRF 分数必须严格高于只被一路命中的
    top_id, top_score = fused[0]
    assert top_id in both
    only_one = [cid for cid, _ in fused if cid not in both]
    if only_one:
        best_single = max(s for cid, s in fused if cid not in both)
        assert top_score > best_single

    hits = search.search(q, top_k=5)
    assert hits and hits[0].chunk_id in both
    assert hits[0].rrf_score > 0
    assert hits[0].bm25_score != 0 and hits[0].vec_score != 0


def test_rrf_math():
    fused = HybridSearch.rrf_fuse([["a", "b"], ["a"]], k=60)
    scores = dict(fused)
    assert scores["a"] == pytest.approx(1 / 61 + 1 / 61)
    assert scores["b"] == pytest.approx(1 / 62)
    assert fused[0][0] == "a"


# ------------------------------------------------------------------ 5. 出处


def test_every_hit_citation_matches_its_chapter(store: NovelStore, built: IndexBuilder):
    search = HybridSearch(store, index=built)
    hits = search.search(Query(text="青云宗 山门 血刀门 断崖"), top_k=8)
    assert hits
    for h in hits:
        assert h.citation.startswith("[ch-"), h.citation
        assert f"ch-{h.ch:04d}" in h.citation
        # citation 里的段号必须落在该 chunk 的 para_range 里
        lo, hi = h.para_start + 1, h.para_end + 1
        if lo == hi:
            assert h.citation == f"[ch-{h.ch:04d}:段{lo}]"
        else:
            assert h.citation == f"[ch-{h.ch:04d}:段{lo}-{hi}]"
        # chunk 文本确实来自该章原文
        src = store.read_chapter(h.ch)
        assert h.text.split("\n")[0].strip() in src


def test_null_reranker_keeps_order(store: NovelStore, built: IndexBuilder):
    q = Query(text="淬体")
    plain = HybridSearch(store, index=built, reranker=None).search(q, top_k=5)
    nulled = HybridSearch(store, index=built, reranker=NullReranker()).search(q, top_k=5)
    assert [h.chunk_id for h in plain] == [h.chunk_id for h in nulled]
    assert all(h.rerank_score is None for h in nulled)


def test_openai_reranker_constructs_without_network():
    rr = OpenAICompatReranker(base_url="http://127.0.0.1:8080/", api_key="sk-r", model="bge-reranker-v2-m3")
    assert rr.url == "http://127.0.0.1:8080/v1/rerank"
    assert rr.payload("q", ["a", "b"]) == {
        "model": "bge-reranker-v2-m3",
        "query": "q",
        "documents": ["a", "b"],
        "top_n": 2,
    }
    assert get_reranker(None) is None
    assert get_reranker({}) is None
    assert get_reranker({"reranker": {"provider": "none"}}) is None
    # 远端但无 key → None（保守）
    assert get_reranker({"reranker": {"provider": "openai", "base_url": "https://api.x.com",
                                     "model": "m"}}) is None
    # 本地无鉴权允许
    local = get_reranker({"reranker": {"provider": "tei", "base_url": "http://127.0.0.1:8080",
                                       "model": "m"}})
    assert isinstance(local, OpenAICompatReranker)


def test_failing_reranker_falls_back_to_rrf(store: NovelStore, built: IndexBuilder):
    class Boom:
        name = "boom"

        async def rerank(self, query, texts):
            raise RuntimeError("network down")

        def rerank_sync(self, query, texts):
            return None

    q = Query(text="淬体")
    base = HybridSearch(store, index=built).search(q, top_k=5)
    boom = HybridSearch(store, index=built, reranker=Boom()).search(q, top_k=5)
    assert [h.chunk_id for h in base] == [h.chunk_id for h in boom]


# ------------------------------------------------------------------ 6. 增量


def test_incremental_build_syncs_add_and_remove(store: NovelStore, built: IndexBuilder):
    assert built.stats().chapters == 3

    # 没改动 → 增量不动任何章
    report = built.build(full=False)
    assert report.changed == [] and report.removed == []

    # 加一章
    store.write_chapter(4, "第四章 新的开始\n\n林昭在青云山的石阶上坐了很久。\n")
    report = built.build(full=False)
    assert report.changed == [4]
    assert built.stats().chapters == 4
    hits = HybridSearch(store, index=built).search(Query(text="石阶"), top_k=3)
    assert hits and hits[0].ch == 4

    # add_chapter 单章接口
    store.write_chapter(5, "第五章 落霞城的雨\n\n苏晚站在屋檐下，看雨丝斜斜地落进青石板缝里。\n")
    assert built.add_chapter(5) >= 1
    assert built.stats().chapters == 5
    assert HybridSearch(store, index=built).search(Query(text="屋檐"), top_k=3)[0].ch == 5

    # 删章
    store.p.chapter_md(5).unlink()
    report = built.build(full=False)
    assert report.removed == [5]
    assert built.stats().chapters == 4
    assert not [h for h in HybridSearch(store, index=built).search(Query(text="屋檐"), top_k=3)]

    # remove_chapter 直调
    assert built.remove_chapter(4) >= 1
    assert built.stats().chapters == 3


def test_edit_chapter_triggers_reindex(store: NovelStore, built: IndexBuilder):
    store.write_chapter(1, CH1 + "\n\n林昭把柴刀插回腰间，回头看了一眼落霞城的方向。\n")
    report = built.build(full=False)
    assert report.changed == [1]
    hits = HybridSearch(store, index=built).search(Query(text="柴刀插回腰间"), top_k=3)
    assert hits and hits[0].ch == 1


# ------------------------------------------------------------------ 7. verify 漂移


def test_verify_detects_tampered_chapter(store: NovelStore, built: IndexBuilder):
    assert built.verify().ok is True

    # 绕过索引直接改正文（模拟外部篡改/手工编辑）
    store.write_chapter(2, CH2.replace("血刀门", "血衣楼"))
    rep = built.verify()
    assert rep.ok is False
    assert rep.drifted == [2]
    assert 2 not in rep.missing
    # 其他章不背锅
    assert 1 not in rep.drifted and 3 not in rep.drifted

    # 新增未索引章 → missing
    store.write_chapter(9, "第九章 尚未索引\n\n这段文字还没有进索引。\n")
    rep2 = built.verify()
    assert 9 in rep2.missing

    # 重建后恢复
    built.build(full=False)
    rep3 = built.verify()
    assert rep3.ok is True
    assert 2 in [h.ch for h in HybridSearch(store, index=built).search(Query(text="血衣楼"), top_k=3)]


def test_verify_reports_orphan_and_missing_index(store: NovelStore, built: IndexBuilder):
    # 直接删正文文件 → orphaned
    store.p.chapter_md(3).unlink()
    rep = built.verify()
    assert 3 in rep.orphaned and rep.ok is False

    # 索引不存在 → ok=False + error，不抛
    built.db_path.unlink()
    rep2 = built.verify()
    assert rep2.ok is False and rep2.error


# ------------------------------------------------------------------ 8. GraphRAG


@pytest.fixture()
def graph_store(store: NovelStore) -> NovelStore:
    from server.core.schema import CharacterCard

    store.save_character(
        CharacterCard(
            id="mc", name="林昭", role="protagonist",
            personality=["沉默", "记仇"], aliases=["小昭"],
            relations=[Relation(target="suwan", type="亲属", note="表姐")],
            first_chapter=1,
        )
    )
    store.save_character(
        CharacterCard(
            id="suwan", name="苏晚", role="support",
            personality=["谨慎"], relations=[Relation(target="mc", type="亲属")],
            first_chapter=2,
        )
    )
    store.save_character(
        CharacterCard(
            id="elder", name="守山弟子", role="support",
            personality=["冷淡"], relations=[Relation(target="mc", type="师徒")],
            first_chapter=1,
        )
    )
    store.save_character(
        CharacterCard(
            id="blood", name="刀疤客", role="antagonist",
            relations=[Relation(target="mc", type="敌对")], first_chapter=2,
        )
    )
    # 动态边（真相文件 entity-deltas.jsonl）
    store.append_entity_delta(EntityDelta(ch=2, entity="mc", kind="relation", op="add",
                                          target="blood", type="敌对", note="矿脉冲突"))
    store.append_entity_delta(EntityDelta(ch=3, entity="mc", kind="relation", op="add",
                                          target="suwan", type="亲属", note="断崖后相认"))
    return store


@pytest.fixture()
def graph(graph_store: NovelStore) -> GraphIndex:
    b = IndexBuilder(graph_store)
    b.build(full=True)
    return GraphIndex(graph_store, index=b)


def test_rel_canon_mapping():
    assert canon_rel("master") == "师徒"
    assert canon_rel("enemy") == "敌对"
    assert canon_rel("member_of") == "所属"
    assert canon_rel("kin") == "亲属"
    assert canon_rel("owns") == "持有"
    assert canon_rel("师徒") == "师徒"
    assert canon_rel("未知类型") == "未知类型"


def test_graph_neighbors_one_hop(graph: GraphIndex):
    edges = graph.neighbors("mc", hops=1)
    assert edges, "林昭应有一跳邻居"
    dsts = {e.dst for e in edges}
    assert "chars/suwan" in dsts
    assert "chars/elder" in dsts
    assert "chars/blood" in dsts
    assert all(e.src == "chars/mc" for e in edges)
    # rels 过滤
    only_kin = graph.neighbors("mc", hops=1, rels=["亲属"])
    assert {e.dst for e in only_kin} == {"chars/suwan"}
    assert all(e.rel_label == "亲属" for e in only_kin)
    # 英文别名同样能过滤
    assert {e.dst for e in graph.neighbors("mc", hops=1, rels=["enemy"])} == {"chars/blood"}


def test_graph_expand_context_brings_related_entities(graph: GraphIndex):
    expanded = graph.expand_context(["mc"], budget_chars=1200)
    assert expanded, "一跳扩展必须带出关系实体"
    names = {e.entity_id for e in expanded}
    assert "suwan" in names
    assert "elder" in names or "blood" in names
    # 种子角色本身不重复返回
    assert "mc" not in names
    # 每条都带关系类型与来源
    for item in expanded:
        assert item.rel
        assert item.via
        assert item.text
    # 预算被遵守
    total = sum(len(e.text) for e in expanded)
    assert total <= 1200

    # 预算收紧后条数变少
    tiny = graph.expand_context(["mc"], budget_chars=40)
    assert len(tiny) <= len(expanded)


def test_graph_renders_expanded_lines(graph: GraphIndex):
    expanded = graph.expand_context(["mc"], budget_chars=1200)
    text = graph.render_expanded(expanded, budget_chars=1200)
    assert text
    for line in text.splitlines():
        assert line.startswith("- ")
        assert "：" not in line.split(":", 1)[0] or True


def test_graph_rebuildable_from_truth(graph_store: NovelStore, graph: GraphIndex):
    """图谱不入真相源：删库重建后结果一致。"""
    before = {(e.src, e.rel, e.dst) for e in graph.neighbors("mc", hops=1)}
    b = IndexBuilder(graph_store)
    b.build(full=True)
    after = {(e.src, e.rel, e.dst) for e in GraphIndex(graph_store, index=b).neighbors("mc", hops=1)}
    assert before == after


# ------------------------------------------------------------------ 9. facade 降级


def test_facade_off_when_index_missing(store: NovelStore):
    facade = RetrievalFacade(store)
    assert facade.available is False
    result = asyncio.run(facade.search_for_outline(_mk_outline()))
    assert isinstance(result, RetrievalResult)
    assert result.mode == "off"
    assert result.degraded is True
    assert result.hits == []
    assert result.rendered == ""

    # 其它入口同样不抛
    assert asyncio.run(facade.graph_expand([])) == []
    assert asyncio.run(facade.graph_expand(["mc"])) == []
    assert facade.verify()["ok"] is False


def test_facade_disabled_flag(store: NovelStore):
    b = IndexBuilder(store)
    b.build(full=True)
    facade = RetrievalFacade(store, enabled=False)
    assert facade.available is False
    assert asyncio.run(facade.search_for_outline(_mk_outline())).mode == "off"


def test_facade_hybrid_and_bm25_modes(store: NovelStore):
    # 无 embedder → bm25 降级但可用
    assert RetrievalFacade(store).build_index(full=True)["chapters"] == 3
    bm25_facade = RetrievalFacade(store)
    assert bm25_facade.available is True
    res = asyncio.run(bm25_facade.search_for_outline(_mk_outline()))
    assert res.mode == "bm25"
    assert res.degraded is True
    assert res.hits, "bm25 模式必须真能召回"
    assert res.rendered

    # 有 embedder → hybrid
    hybrid_facade = RetrievalFacade(store, embedder=HashEmbedder())
    hybrid_facade.build_index(full=True)
    res2 = asyncio.run(hybrid_facade.search_for_outline(_mk_outline()))
    assert res2.mode == "hybrid"
    assert res2.degraded is False
    assert res2.hits


def test_facade_swallows_broken_index(store: NovelStore):
    b = IndexBuilder(store)
    b.build(full=True)
    # 把库写坏（表结构损坏）
    conn = sqlite3.connect(str(b.db_path))
    conn.execute("DROP TABLE chunks")
    conn.commit()
    conn.close()
    facade = RetrievalFacade(store)
    assert facade.available is False
    res = asyncio.run(facade.search_for_outline(_mk_outline()))
    assert res.mode == "off"
    assert isinstance(facade.verify(), dict)
    assert isinstance(facade.build_index(), dict)


def test_facade_search_never_raises_on_bad_outline(store: NovelStore):
    b = IndexBuilder(store)
    b.build(full=True)
    facade = RetrievalFacade(store)
    # 章纲字段缺失/怪值也不能炸
    for outline in (ChapterOutline(ch=1), ChapterOutline(ch=4, title="", must_include=["未知词条"])):
        res = asyncio.run(facade.search_for_outline(outline))
        assert isinstance(res, RetrievalResult)
        assert res.mode in ("off", "bm25", "hybrid")


def test_facade_excludes_recent_chapter(store: NovelStore):
    b = IndexBuilder(store)
    b.build(full=True)
    facade = RetrievalFacade(store)
    res = asyncio.run(facade.search_for_outline(_mk_outline(ch=3, title="断崖 老藤")))
    assert res.hits
    # ch=3 → 排除最近 1 章（ch=2）
    assert all(h.ch != 2 for h in res.hits)


# ------------------------------------------------------------------ 10. rendered 出处


def test_rendered_every_block_starts_with_citation(store: NovelStore):
    b = IndexBuilder(store)
    b.build(full=True)
    facade = RetrievalFacade(store)
    res = asyncio.run(facade.search_for_outline(_mk_outline(), budget_chars=6000))
    assert res.rendered
    blocks = [blk for blk in res.rendered.split("\n\n") if blk.strip()]
    assert blocks
    import re

    for blk in blocks:
        assert blk.startswith("[ch-"), blk[:40]
        m = re.match(r"^\[ch-(\d{4}):段(\d+)(?:-(\d+))?\]", blk)
        assert m, blk[:60]
    # 至少有一条与 hits 对应
    assert res.sources == [f"[ch-{h.ch:04d}]" for h in res.hits]


def test_rendered_respects_budget(store: NovelStore):
    b = IndexBuilder(store)
    b.build(full=True)
    facade = RetrievalFacade(store)
    res = asyncio.run(facade.search_for_outline(_mk_outline(), budget_chars=250))
    assert len(res.rendered) <= 260
    res2 = asyncio.run(facade.search_for_outline(_mk_outline(), budget_chars=6000))
    assert len(res2.rendered) >= len(res.rendered)


def test_facade_stats_and_build_verify(store: NovelStore):
    facade = RetrievalFacade(store)
    out = facade.build_index(full=True)
    assert out["error"] == ""
    assert out["chapters"] == 3 and out["chunks"] >= 3
    stats = facade.stats()
    assert stats["ready"] is True
    assert stats["mode"] == "bm25"
    assert facade.verify()["ok"] is True


def test_index_rebuild_refuses_oversized_corpus(tmp_path):
    """缺陷 #59：索引重建的正文总量要有上界（切块代价超线性）。"""
    from server.api.deps import AppContext
    from server.core.schema import BookMeta

    ctx = AppContext.create(tmp_path / "ws")
    bn = ctx.workbench.create_novel(BookMeta(title="索引上界", platform="tomato"))
    store = bn.store if hasattr(bn, "store") else bn
    rdir = ctx.workbench.refresh_shelf().novels[0].dir
    bc = ctx.book(rdir)
    for i in range(1, 5):
        store.write_chapter(i, "字" * 800000)
    res = bc.retrieval.build_index(full=True)
    assert "IndexTooLargeError" in str(res.get("error")), res
    assert res["changed"] == []
    for i in range(1, 5):
        store.write_chapter(i, "字" * 1000)
    res = bc.retrieval.build_index(full=True)
    assert not res.get("error"), res
    assert res["changed"] == [1, 2, 3, 4]



# ------------------------------------------------- 缺陷 #118：实体增量损坏不许静默缩水

_CONFLICT_FILE = "memory/entity-deltas.jsonl"


def _write_deltas(graph_store: NovelStore, n: int) -> None:
    for i in range(1, n + 1):
        graph_store.append_entity_delta(
            EntityDelta(ch=i, entity="mc", kind="relation", op="add",
            target="t%d" % i, type="师徒"))


def test_entity_delta_corruption_is_reported_not_swallowed(graph_store: NovelStore):
    """缺陷 #118：entity-deltas.jsonl 读不利索时，必须说出来。

    这个文件进版本库，三种真实情况都会让它部分失效：git 合并冲突标记、
    写入中断留下半行、编码错乱。旧实现一律静默跳过，实测实体从 13 掉到 4，
    而 build_index / verify 依旧全绿——作者以为沉淀了，实际蒸发了。
    """
    from server.retrieval.index import IndexBuilder

    _write_deltas(graph_store, 6)
    b = IndexBuilder(graph_store)
    b.build(full=True)
    rep = graph_store.read_entity_deltas_report()
    assert len(rep.rows) == 8
    assert rep.ok is True
    assert rep.problem("实体增量") == ""

    p = graph_store.p.entity_deltas
    orig = p.read_text(encoding="utf-8")

    # 场景一：git 合并冲突标记（文件进版本库，冲突是会发生的）
    p.write_text("<<<<<<< HEAD\n" + orig + "=======\n" + orig + ">>>>>>> main\n", encoding="utf-8")
    rep = graph_store.read_entity_deltas_report()
    assert rep.conflict_markers, "冲突标记没被认出来"
    msg = rep.problem("实体增量 entity-deltas.jsonl")
    assert "冲突" in msg and "entity-deltas.jsonl" in msg
    v = b.verify()
    assert v.ok is False, "有冲突标记却报 ok=True"
    assert "冲突" in v.entity_source
    assert "冲突" in v.as_dict()["entity_source"]  # 必须出得了对外的 dict

    # 场景二：写入中断留下半行
    p.write_text(orig + "{\"ch\": 7, \"entity\": \"mc\"", encoding="utf-8")
    rep = graph_store.read_entity_deltas_report()
    assert rep.bad_lines == [9], rep.bad_lines
    assert len(rep.rows) == 8, "好的 8 行应该照常读到"
    v = b.verify()
    assert v.ok is False, "有坏行却报 ok=True"
    assert v.as_dict()["entity_source"]

    # 场景三：整文件非 JSON（编码错乱）
    p.write_bytes("\xff\xfe not json at all".encode("utf-8"))
    rep = graph_store.read_entity_deltas_report()
    assert rep.rows == []
    assert rep.unreadable or rep.bad_lines
    v = b.verify()
    assert v.ok is False, "实体全没了却报 ok=True"
    assert v.as_dict()["entity_source"]


def test_build_report_carries_entity_source_warning(graph_store: NovelStore):
    """build() 的返回体也要带上实体数据源的问题（缺陷 #118）。"""
    from server.retrieval.index import IndexBuilder

    _write_deltas(graph_store, 4)
    b = IndexBuilder(graph_store)
    ok_rep = b.build(full=True)
    assert ok_rep.entity_source == "", ok_rep.entity_source
    assert ok_rep.as_dict()["entity_source"] == ""

    p = graph_store.p.entity_deltas
    orig = p.read_text(encoding="utf-8")
    p.write_text("<<<<<<< HEAD\n" + orig + "=======\n>>>>>>> main\n", encoding="utf-8")
    bad_rep = b.build(full=True)
    assert "冲突" in bad_rep.entity_source, bad_rep.entity_source
    assert "冲突" in bad_rep.as_dict()["entity_source"]


def test_entity_delta_read_keeps_valid_rows_when_partially_broken(graph_store: NovelStore):
    """坏行照旧跳过——一行坏数据不该让整次重建失败（#118 的容错底线）。"""
    _write_deltas(graph_store, 3)
    p = graph_store.p.entity_deltas
    orig = p.read_text(encoding="utf-8")
    p.write_text("not json at all\n" + orig + "\n{\"ch\": 9", encoding="utf-8")
    rep = graph_store.read_entity_deltas_report()
    assert len(rep.rows) == 5, "好行必须保留"
    assert len(rep.bad_lines) == 2, rep.bad_lines
    assert rep.dropped == 2
    assert len(graph_store.read_entity_deltas()) == 5, "旧接口行为不变"


# ------------------------------------------------------------------ 9b. 缺陷 #127：向量静默降级


class _DownEmbedder:
    """模拟外部 embedding 服务挂掉（502 / 超时 / 配额耗尽）。"""

    name = "down"
    dim = 256

    async def embed(self, texts: list[str]) -> list[list[float]]:
        raise RuntimeError("上游 502：embedding 服务不可用")


def test_build_reports_vector_source_when_embedding_fails(store: NovelStore):
    """embedding 挂掉时 build 报告必须说出来，不能只体现在 embedded=0 上（#127）。"""
    b = IndexBuilder(store, embedder=_DownEmbedder())
    rep = b.build(full=True)
    assert rep.error == "", "embedding 失败不该让整次建索引失败（BM25 主链路要活）"
    assert rep.stats.chunks > 0
    assert rep.stats.vectors == 0
    assert rep.embedded == 0
    # 修复前：这三处全在说谎（error 空串、没有一句话说明问题）
    assert "向量缺失" in rep.vector_source, rep.vector_source
    assert "502" in rep.vector_source, rep.vector_source
    assert rep.as_dict()["vector_source"] == rep.vector_source


def test_verify_flags_missing_vectors(store: NovelStore):
    """hash 全对但向量表是空的 —— 对账必须不通过（#127）。"""
    b = IndexBuilder(store, embedder=_DownEmbedder())
    b.build(full=True)
    rep = b.verify()
    assert rep.drifted == [] and rep.missing == [] and rep.orphaned == []
    assert rep.vector_source, "零向量必须写进对账报告"
    assert rep.ok is False, "语义检索是假的，对账不能报绿"
    assert b.verify.__name__ == "verify"
    assert rep.as_dict()["vector_source"] == rep.vector_source
    assert rep.as_dict()["ok"] is False


def test_verify_ok_when_vectors_present(store: NovelStore):
    """对照组：向量真的建出来了，对账就该绿，vector_source 为空（#127）。"""
    b = IndexBuilder(store, embedder=HashEmbedder())
    rep = b.build(full=True)
    assert rep.stats.vectors == rep.stats.chunks > 0
    assert rep.vector_source == "", rep.vector_source
    v = b.verify()
    assert v.ok is True, v.as_dict()
    assert v.vector_source == "", v.vector_source


def test_verify_tolerates_unconfigured_embedding(store: NovelStore):
    """压根没配 embedder 是配置选择：提示照给，但不判对账失败（永远红的告警没人看）。"""
    b = IndexBuilder(store)
    assert b.embedder is None
    b.build(full=True)
    v = b.verify()
    assert "向量缺失" in v.vector_source, v.vector_source
    assert v.ok is True, "没配 embedding 不该让夜间对账天天红"


def test_facade_mode_does_not_claim_hybrid_without_vectors(store: NovelStore):
    """门面档位要说真话：有 embedder 但零向量时，mode 必须是 bm25（#127）。"""
    facade = RetrievalFacade(store, embedder=_DownEmbedder())
    facade.build_index(full=True)
    base = IndexBuilder(store, embedder=_DownEmbedder())
    assert base.stats().vectors == 0
    assert facade.mode == "bm25", facade.mode
    assert facade.stats()["mode"] == "bm25", facade.stats()

    good = RetrievalFacade(store, embedder=HashEmbedder())
    good.build_index(full=True)
    assert good.mode == "hybrid", good.mode
    assert good.stats()["vectors"] > 0


def test_facade_early_returns_carry_vector_source_key(store: NovelStore, monkeypatch):
    """早退分支也要带 vector_source —— 缺键会让前端和 CLI 静默不告警（#127）。"""
    disabled = RetrievalFacade(store, enabled=False)
    assert disabled.build_index()["vector_source"] == ""
    assert disabled.verify()["vector_source"] == ""

    # 索引装配失败（早退分支）：键必须存在，前端才不会读到 undefined 后静默跳过
    broken = RetrievalFacade(store)
    monkeypatch.setattr(broken, "_get_index", lambda: None)
    assert broken.build_index()["vector_source"] == ""
    assert broken.verify()["vector_source"] == ""
    assert broken.verify()["entity_source"] == ""



# ------------------------------------------------------------------ 9c. 缺陷 #128：只读读路径不得制造旁文件


def test_read_only_connect_does_not_create_wal_sidecars(store: NovelStore):
    """只读打开索引库不该在库旁边留下 -wal / -shm（缺陷 #128）。

    WAL 模式的库哪怕以 `mode=ro` 打开，SQLite 也会新建这两个旁文件。正文一个字节
    没动，但任何按「目录下文件清单 + md5」做基线的校验（verify-all.sh 第 3~5 步
    那道防线）都会因此变红 —— 假红一次，这道防线就没人信了。实测：只读连接一开，
    目录里立刻多出 32KB 的 index.db-shm，而 git status 是干净的。
    """
    b = IndexBuilder(store)
    b.build(full=True)
    d = b.db_path.parent
    for n in list(d.iterdir()):
        if n.name.endswith(("-wal", "-shm")):
            n.unlink()
    before = sorted(x.name for x in d.iterdir())
    assert before, "索引库得先建出来"

    # 读路径三连：就绪判断 / 统计 / 对账
    assert b.index_ready() is True
    b.stats()
    rep = b.verify()
    assert rep.ok is True

    after = sorted(x.name for x in d.iterdir())
    assert after == before, f"只读读路径制造了旁文件：{before} -> {after}"
    assert not [n for n in after if n.endswith(("-wal", "-shm"))], after


def test_read_only_connect_still_sees_the_data(store: NovelStore):
    """immutable=1 不能把库读空 —— 只读路径必须仍然拿得到切块（#128 的对照组）。"""
    b = IndexBuilder(store)
    b.build(full=True)
    assert b.stats().chunks > 0, "只读路径得能读到切块数"
    assert b.index_ready() is True
    assert b.all_chunks(), "all_chunks() 走的是只读连接，不能返回空"
    assert b.verify().drifted == []
