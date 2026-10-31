"""agents 层单测：Planner / Writer / Reviewer / Curator / Deconstructor 全链路。"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from server.agents.curator import Curator
from server.agents.deconstructor import Deconstructor, heuristic_analysis, split_chapters
from server.agents.planner import Planner, confirm_outline
from server.agents.reviewer import Reviewer, AI_SMELL_WORDS
from server.agents.writer import Writer, clean_prose
from server.context.assembler import ContextAssembler
from server.core.schema import (
    BookMeta,
    ChapterOutline,
    CharacterCard,
    ForeshadowAction,
    ForeshadowBook,
    ForeshadowItem,
    Proposal,
    PowerScale,
    Scene,
)
from server.core.store import NovelStore, StoreError, Workbench, count_cn_words
from server.model.base import RouteHop, RoutingTable
from server.model.gateway import ModelGateway
from server.model.providers.mock import MockProvider
from server.model.providers.registry import ProviderRegistry, ProviderSpec


# ---------------------------------------------------------------- fixtures


def _gw(tmp_path: Path) -> ModelGateway:
    reg = ProviderRegistry()
    for name in ("mock-local", "planner-mock", "writer-mock"):
        reg.providers[name] = MockProvider(name=name)
        reg.specs[name] = ProviderSpec(name=name, kind="mock", models=["mock-1"])
    table = RoutingTable(
        routes={
            "plan": [RouteHop("planner-mock", "mock-1")],
            "write": [RouteHop("writer-mock", "mock-1")],
            "review": [RouteHop("mock-local", "mock-1")],
            "curator": [RouteHop("mock-local", "mock-1")],
            "deconstruct": [RouteHop("mock-local", "mock-1")],
            "free": [RouteHop("mock-local", "mock-1")],
        }
    )
    return ModelGateway(tmp_path / "cfg", registry=reg, routing=table, raw_routing={})


@pytest.fixture()
def store(tmp_path: Path) -> NovelStore:
    wb = Workbench(tmp_path / "ws")
    s = wb.create_novel(BookMeta(title="测试小说", genre="玄幻", platform="tomato"))
    s.save_character(
        CharacterCard(
            id="mc",
            name="林澈",
            aliases=["小澈"],
            personality=["外冷内热"],
            speech_habits=["无妨。"],
            boundaries=["绝不主动屠杀平民"],
            power_scale=PowerScale(rank="淬体七重", ceiling="淬体九重", forbidden="本卷内不得突破凝气期"),
        )
    )
    s.save_character(CharacterCard(id="elder-wang", name="王长老", role="antagonist"))
    return s


# ---------------------------------------------------------------- Planner


def test_planner_generates_and_saves_outlines(store: NovelStore, tmp_path: Path):
    gw = _gw(tmp_path)
    p = Planner(store, gw)
    res = asyncio.run(p.plan(start_ch=1, count=3))
    assert len(res.chapters) == 3
    assert [c.ch for c in res.chapters] == [1, 2, 3]
    assert res.saved == 3
    assert all(c.hook for c in res.chapters)  # 每章必含 hook
    assert all(c.scenes for c in res.chapters)
    assert all(c.status == "draft" for c in res.chapters)  # 草案
    # 落盘
    assert store.load_outline(1) is not None


def test_planner_continues_from_last_chapter(store: NovelStore, tmp_path: Path):
    store.write_chapter(5, "x" * 100)
    book = store.load_book()
    book.current_chapter = 5
    store.save_book(book)
    p = Planner(store, _gw(tmp_path))
    res = asyncio.run(p.plan(count=2))
    assert res.chapters[0].ch == 6


def test_planner_skips_final_chapters(store: NovelStore, tmp_path: Path):
    o = ChapterOutline(ch=1, title="已定稿", hook="h", status="final")
    store.save_outline(o)
    p = Planner(store, _gw(tmp_path))
    asyncio.run(p.plan(start_ch=1, count=1))
    assert store.load_outline(1).status == "final"


def test_planner_validates_power_events(store: NovelStore, tmp_path: Path):
    """模型若产出违规 power_event，Planner 必须报警。"""
    from server.core.domain import validate_power_events
    from server.core.schema import PowerEvent

    o = ChapterOutline(ch=9, power_events=[PowerEvent(char="mc", to="凝气期")])
    issues = validate_power_events(o, store.character_map())
    assert issues and issues[0].level == "error"


def test_confirm_outline(store: NovelStore):
    store.save_outline(ChapterOutline(ch=1, title="x", hook="h", status="draft"))
    o = confirm_outline(store, 1)
    assert o.status == "planned"


# 缺陷 #72：confirm 原先是「无条件写成 planned」，published / final 也能被拉回来。
# 确认是向前推进，不是倒回——拉回 planned 会让 daily-panel 的存稿口径凭空多一章。
def test_confirm_outline_refuses_to_rewind_terminal_states(store: NovelStore):
    """只有 draft / planned / writing 能被确认回 planned；终态一律拒。"""
    for ch, s in enumerate(["draft", "planned", "writing"], start=1):
        store.save_outline(ChapterOutline(ch=ch, title="x", hook="h", status=s))
        assert confirm_outline(store, ch).status == "planned", s + " 应可确认"

    for ch, s in enumerate(["drafted", "reviewed", "final", "published"], start=11):
        store.save_outline(ChapterOutline(ch=ch, title="x", hook="h", status=s))
        with pytest.raises(ValueError) as ei:
            confirm_outline(store, ch)
        assert "非法状态迁移" in str(ei.value), s + " 应被拒"
        # 被拒后章纲必须原样躺在盘上，不能改了一半
        assert store.load_outline(ch).status == s, s + " 被改写"

    # 不存在的章仍然是原来的 ValueError
    with pytest.raises(ValueError) as ei:
        confirm_outline(store, 999)
    assert "章纲不存在" in str(ei.value)


# ---------------------------------------------------------------- Writer


def test_writer_generates_prose_and_persists(store: NovelStore, tmp_path: Path):
    gw = _gw(tmp_path)
    store.save_outline(
        ChapterOutline(
            ch=1,
            title="淬骨",
            pov="mc",
            scenes=[Scene(loc="青岩矿脉", chars=["mc"], goal="夺矿", conflict="使绊", outcome="险胜")],
            must_include=["@characters/mc"],
            hook="矿洞深处传来诵经声",
            target_words=(800, 1200),
            status="planned",
        )
    )
    w = Writer(store, gw, ContextAssembler(store))
    res = asyncio.run(w.write(1))
    assert res.words > 200
    assert store.chapter_exists(1)
    assert res.assembled_path == "memory/ch-0001.assembled.md"
    assert store.p.assembled_md(1).exists()
    # 章状态推进
    assert store.load_outline(1).status == "drafted"
    assert store.load_book().current_chapter == 1


def test_writer_stream(store: NovelStore, tmp_path: Path):
    gw = _gw(tmp_path)
    store.save_outline(
        ChapterOutline(ch=2, title="试锋", pov="mc", hook="有人叫住了他", scenes=[Scene(goal="试剑")], target_words=(600, 900), status="planned")
    )
    w = Writer(store, gw)

    async def run():
        chunks = []
        async for c in w.write_stream(2):
            chunks.append(c)
        return chunks

    chunks = asyncio.run(run())
    assert chunks[-1].done
    assert chunks[-1].usage["words"] > 100
    assert store.chapter_exists(2)


def test_writer_missing_outline_raises(store: NovelStore, tmp_path: Path):
    w = Writer(store, _gw(tmp_path))
    with pytest.raises(ValueError):
        asyncio.run(w.write(99))


def test_writer_multi_version_and_adopt(store: NovelStore, tmp_path: Path):
    gw = _gw(tmp_path)
    store.save_outline(
        ChapterOutline(ch=3, title="版本", pov="mc", hook="h", scenes=[Scene(goal="g")], target_words=(300, 500), status="planned")
    )
    w = Writer(store, gw)
    asyncio.run(w.write(3))
    asyncio.run(w.write(3, as_version=True))
    vs = store.list_versions(3)
    assert len(vs) == 1  # v1 是 as_version=True 的那版
    res = w.adopt_version(3, 1)
    assert res["adopted"] == 1
    # 切换后原正文被留档
    assert len(store.list_versions(3)) >= 1




# ------------------------------------------------- 缺陷 #76：切换版本也要查 published


def test_adopt_version_refuses_published(store: NovelStore, tmp_path: Path):
    """切换版本最终是 write_chapter——和 write()/write_stream() 同一个落盘动作。

    实测（修复前）：章纲 status=published 时 adopt_version 照样成功，正文被换成
    旧版本内容、章纲仍停在 published；同一章走 write() 却被 Writer 拒绝。
    """
    store.save_outline(
        ChapterOutline(ch=5, title="版本", pov="mc", hook="h", scenes=[Scene(goal="g")])
    )
    store.write_chapter(5, "当前正文在这里，够长就算一章。")
    store.write_version(5, 1, "旧版本正文在这里，也够长。", {"model": "x"})
    w = Writer(store, _gw(tmp_path))

    # 非终态：正常可切换
    res = w.adopt_version(5, 1)
    assert res["adopted"] == 1

    # 置成 published 后必须拒绝
    o = store.load_outline(5)
    o.status = "published"
    store.save_outline(o)
    before = store.read_chapter(5)
    with pytest.raises(ValueError) as ei:
        w.adopt_version(5, 1)
    assert "已发布" in str(ei.value), str(ei.value)
    assert store.read_chapter(5) == before, "被拒后正文必须原样"

def test_clean_prose_strips_fences():
    assert clean_prose("```markdown\n# 第1章\n正文\n```") == "# 第1章\n正文"
    assert clean_prose("好的，以下是正文：\n\n# 第1章") == "# 第1章"


def test_score_version(store: NovelStore, tmp_path: Path):
    gw = _gw(tmp_path)
    store.save_outline(ChapterOutline(ch=4, title="评分", hook="h", scenes=[Scene(goal="g")], status="planned"))
    w = Writer(store, gw)
    asyncio.run(w.write(4, as_version=True))
    r = w.score_version(4, 1, 5, "节奏好")
    assert r["score"] == 5
    with pytest.raises(ValueError):
        w.score_version(4, 1, 9)


# ---------------------------------------------------------------- Reviewer


def _ready_chapter(store: NovelStore, ch: int = 1, text: str | None = None) -> str:
    body = text or ("# 第1章 淬骨\n\n" + "他握紧了拳，向后退了半步。石壁上有一道细纹，像是被人刻过。" * 40 + "\n\n就在这时，矿洞深处传来诵经声。")
    store.write_chapter(ch, body)
    store.save_outline(
        ChapterOutline(ch=ch, title="淬骨", pov="mc", hook="矿洞深处传来诵经声", scenes=[Scene(goal="夺矿")], status="drafted")
    )
    return body


def test_reviewer_rule_layer_detects_word_count(store: NovelStore, tmp_path: Path):
    _ready_chapter(store, 1, "太短了。")
    r = Reviewer(store, _gw(tmp_path))
    res = asyncio.run(r.review(1, use_model=False))
    cats = {i.category for i in res.report.issues}
    assert "words" in cats


def test_reviewer_detects_redline(store: NovelStore, tmp_path: Path):
    _ready_chapter(store, 2, "他选择了自杀。" + "正文内容。" * 500)
    r = Reviewer(store, _gw(tmp_path))
    res = asyncio.run(r.review(2, use_model=False))
    assert any(i.category == "redline" and i.severity == "high" for i in res.report.issues)


def test_reviewer_detects_ai_flavor(store: NovelStore, tmp_path: Path):
    body = "他仿佛看见了什么，仿佛世界都变了，仿佛一切都静止。" * 30
    _ready_chapter(store, 3, body)
    r = Reviewer(store, _gw(tmp_path))
    res = asyncio.run(r.review(3, use_model=False))
    assert any(i.category == "ai-flavor" for i in res.report.issues)


def test_reviewer_detects_pov_break(store: NovelStore, tmp_path: Path):
    body = "他走进矿洞。" * 100 + "他不知道的是，远处有人在看着他。"
    _ready_chapter(store, 4, body)
    r = Reviewer(store, _gw(tmp_path))
    res = asyncio.run(r.review(4, use_model=False))
    assert any(i.category == "pov" for i in res.report.issues)


def test_reviewer_detects_forbidden_power(store: NovelStore, tmp_path: Path):
    body = "他一步踏入凝气期，气息暴涨。" + "石壁震颤。" * 300
    _ready_chapter(store, 5, body)
    r = Reviewer(store, _gw(tmp_path))
    res = asyncio.run(r.review(5, use_model=False))
    assert any(i.category == "power" and i.severity == "high" for i in res.report.issues)


def test_reviewer_writes_report_and_advances_status(store: NovelStore, tmp_path: Path):
    _ready_chapter(store, 6)
    r = Reviewer(store, _gw(tmp_path))
    res = asyncio.run(r.review(6))
    assert res.path == "memory/ch-0006.review.json"
    report = store.load_review(6)
    assert report is not None and report.ch == 6
    assert "counts" in report.stats
    assert store.load_outline(6).status == "reviewed"


def test_reviewer_repetition_across_chapters(store: NovelStore, tmp_path: Path):
    same = "他抬头看向远处的山门，风雪正紧，钟声在山谷里回荡了很久很久。" * 20
    store.write_chapter(7, same)
    store.write_chapter(8, same)
    store.save_outline(ChapterOutline(ch=8, title="复读", hook="h", scenes=[Scene(goal="g")], status="drafted"))
    r = Reviewer(store, _gw(tmp_path))
    res = asyncio.run(r.review(8, use_model=False))
    assert any(i.category == "repetition" for i in res.report.issues)


def test_reviewer_model_failure_degrades(store: NovelStore, tmp_path: Path):
    _ready_chapter(store, 9)
    reg = ProviderRegistry()
    reg.providers["bad"] = MockProvider(fail_times=99, fail_with="bridge_down", name="bad")
    reg.specs["bad"] = ProviderSpec(name="bad", kind="mock", models=["m"])
    gw = ModelGateway(
        tmp_path / "cfg2",
        registry=reg,
        routing=RoutingTable(routes={"review": [RouteHop("bad", "m")]}),
        raw_routing={},
    )
    r = Reviewer(store, gw)
    res = asyncio.run(r.review(9))  # 不抛
    assert "model failed" in res.report.model


# ---------------------------------------------------------------- Curator


def test_curator_produces_proposals_not_writes(store: NovelStore, tmp_path: Path):
    _ready_chapter(store, 10, "# 第10章\n\n" + "正文。" * 500 + "\n\n玄历1024年四月初二，他离开了矿脉。")
    store.save_outline(
        ChapterOutline(
            ch=10,
            title="离矿",
            pov="mc",
            hook="h",
            scenes=[Scene(goal="g")],
            foreshadow_actions=[{"ref": "F-001", "action": "hint", "note": "矿壁"}],
            status="reviewed",
        )
    )
    store.save_foreshadow(
        ForeshadowBook(items=[ForeshadowItem(id="F-001", desc="残图", planted_ch=3, status="planted", deadline_ch=40)])
    )
    before = store.read_truth_text("world-state.md")
    c = Curator(store, _gw(tmp_path))
    res = asyncio.run(c.curate(10))
    assert res.pack.proposals
    files = {p.file for p in res.pack.proposals}
    assert any("summary" in f for f in files)
    # 提案阶段不写盘
    assert store.read_truth_text("world-state.md") == before
    assert not store.read_summary(10)
    # 预览有 diff
    assert any(v.startswith("---") or v.startswith("+++") or v for v in res.previews.values())


def test_curator_confirm_applies_and_commits(store: NovelStore, tmp_path: Path):
    from server.core.gitbot import GitBot

    GitBot(store.root).init()
    _ready_chapter(store, 11)
    store.save_outline(ChapterOutline(ch=11, title="确认", pov="mc", hook="h", scenes=[Scene(goal="g")], status="reviewed"))
    c = Curator(store, _gw(tmp_path))
    res = asyncio.run(c.curate(11, use_model=False))
    out = c.confirm(11, commit=True)
    assert out["applied"]
    assert store.read_summary(11)  # 落盘了
    assert store.load_outline(11).status == "final"
    assert out["commit"]["ok"]


def test_curator_rejects_out_of_whitelist(store: NovelStore, tmp_path: Path):
    _ready_chapter(store, 12)
    c = Curator(store, _gw(tmp_path))
    warnings: list[str] = []
    props = c._to_proposals(
        {"proposals": [{"file": "server/hack.py", "op": "create", "content": "evil"}]}, 12, warnings
    )
    assert props == []
    assert any("白名单" in w for w in warnings)


def test_curator_partial_confirm(store: NovelStore, tmp_path: Path):
    _ready_chapter(store, 13)
    store.save_outline(ChapterOutline(ch=13, title="部分", pov="mc", hook="h", scenes=[Scene(goal="g")], status="reviewed"))
    c = Curator(store, _gw(tmp_path))
    res = asyncio.run(c.curate(13, use_model=False))
    first_id = res.pack.proposals[0].id
    out = c.confirm(13, accepted_ids=[first_id], commit=False)
    assert len(out["applied"]) == 1
    assert len(out["skipped"]) == len(res.pack.proposals) - 1


# ---------------------------------------------------------------- Deconstructor


def test_split_chapters_by_heading():
    raw = "第一章 开局\n" + "正文内容。" * 100 + "\n\n第二章 试锋\n" + "更多内容。" * 100 + "\n\n第三章 破境\n" + "结局。" * 100
    chapters = split_chapters(raw)
    assert len(chapters) == 3
    assert chapters[0].words > 100


def test_split_chapters_fallback_hard_split():
    raw = "没有章节标记的文本。" * 2000
    chapters = split_chapters(raw)
    assert len(chapters) >= 2


def test_heuristic_analysis_shapes():
    raw = "第一章\n" + "他猛地冲出，剑光一闪。突然，危险逼近！" * 50
    chapters = split_chapters(raw)
    h = heuristic_analysis(chapters)
    assert "hook_ratio" in h and "coolpoint_curve" in h
    assert set(h["hook_ratio"]) == {"危机型", "悬念型", "反转型", "情绪型"}


def test_deconstructor_products(store: NovelStore, tmp_path: Path):
    raw = "\n\n".join(
        f"第{i}章 标题{i}\n" + ("他猛地冲出，剑光一闪。突然，危险逼近！" * 60) for i in range(1, 13)
    )
    d = Deconstructor(store, _gw(tmp_path))
    res = asyncio.run(d.analyze(title="对标书A", content=raw))
    base = store.p.root / "analysis" / "benchmarks" / res.id
    for f in ("structure.json", "pacing.md", "hooks.md", "template.json"):
        assert (base / f).exists(), f
    # 版权红线：产物不含原文整句
    assert "剑光一闪" not in (base / "structure.json").read_text(encoding="utf-8")
    assert (store.p.root / "analysis" / "templates" / f"{res.id}.json").exists()


def test_deconstructor_heuristic_only(store: NovelStore, tmp_path: Path):
    raw = "\n\n".join(f"第{i}章\n" + ("内容。" * 300) for i in range(1, 6))
    d = Deconstructor(store, None)
    res = asyncio.run(d.analyze(title="无模型", content=raw, use_model=False))
    assert res.chapters == 5
    assert res.products


# ---------------------------------------------------------------- 证据校验（防幻觉）

def test_reviewer_drops_model_issue_with_fabricated_evidence(store: NovelStore, tmp_path: Path):
    """模型把 prompt 里的「禁用词」当成正文命中时必须被丢弃。

    这是实测复现过的真实缺陷：文风指纹写着「忌：眼中闪过一丝」，
    模型就报一条 high 级 ai-flavor 命中，而正文里根本没有这个词。
    """
    _ready_chapter(store, 14, "石阶上结着薄冰。" * 300)
    r = Reviewer(store, _gw(tmp_path))
    res = asyncio.run(r.review(14))  # 默认启用模型
    # 规则层不会凭空说正文里有「眼中闪过一丝」
    for issue in res.report.issues:
        if issue.category in ("ai-flavor", "repetition", "redline", "OOC", "power", "timeline"):
            assert issue.evidence, "有 category 必须有 evidence"
            assert issue.evidence[:6] in store.read_chapter(14) or "丢弃" in res.report.model, (
                f"证据不在正文中：{issue.category} / {issue.evidence!r}"
            )


def test_verify_evidence_filters_hallucinations():
    from server.agents.reviewer import _verify_evidence
    from server.core.schema import ReviewIssue

    body = "# 第1章\n\n他握紧了拳，向后退了半步。"
    good = ReviewIssue(category="ai-flavor", evidence="握紧了拳", suggestion="x")
    bad = ReviewIssue(category="ai-flavor", evidence="眼中闪过一丝", suggestion="x")
    desc = ReviewIssue(category="hook", evidence="章末平收", suggestion="x")  # 描述类，不要求字面
    kept, rejected = _verify_evidence([good, bad, desc], body)
    assert good in kept
    assert bad in rejected
    assert desc in kept


def test_verify_evidence_empty_rejected():
    from server.agents.reviewer import _verify_evidence
    from server.core.schema import ReviewIssue

    kept, rejected = _verify_evidence([ReviewIssue(category="OOC", evidence="", suggestion="x")], "正文")
    assert not kept and len(rejected) == 1


# ------------------------------------------------- 结构性提案兜底（伏笔不丢）

def test_curator_backfills_foreshadow_when_model_omits_it(store: NovelStore, tmp_path: Path):
    """回归：模型只产出摘要+deltas 时，章纲里的伏笔动作必须仍被提案。

    实测缺陷：mock/真实模型都可能漏掉伏笔提案，导致伏笔池永远是空的，
    状态机（plant→hint→resolve）在真实使用中根本不生效。
    """
    _ready_chapter(store, 21, "他握紧矿牌，向岔道口走去。" * 200)
    o = store.load_outline(21)
    o.foreshadow_actions = [ForeshadowAction(ref="F-001", action="plant", note="矿牌上的编号")]
    store.save_outline(o)

    c = Curator(store, _gw(tmp_path))
    res = asyncio.run(c.curate(21))
    fs = [p for p in res.pack.proposals if p.file == "truth/foreshadow.json"]
    assert fs, "模型漏掉伏笔提案时必须由章纲补齐"
    assert any("F-001" in json.dumps(p.patch or {}, ensure_ascii=False) for p in fs)


def test_confirm_plant_then_hint_moves_state_machine(store: NovelStore, tmp_path: Path):
    """端到端：确认 plant 后伏笔进池，再确认 hint 后状态变 hinted。"""
    _ready_chapter(store, 22, "他把矿牌按在石壁上。" * 200)
    o = store.load_outline(22)
    o.foreshadow_actions = [ForeshadowAction(ref="F-009", action="plant", note="石壁上的刻痕")]
    store.save_outline(o)
    c22 = Curator(store, _gw(tmp_path))
    asyncio.run(c22.curate(22))
    c22.confirm(22, commit=False)

    fb = store.load_foreshadow()
    item = fb.get("F-009")
    assert item is not None and item.status == "planted"
    assert item.planted_ch == 22

    _ready_chapter(store, 23, "刻痕比上次多了一道。" * 200)
    o2 = store.load_outline(23)
    o2.foreshadow_actions = [ForeshadowAction(ref="F-009", action="hint", note="多了一道")]
    store.save_outline(o2)
    c = Curator(store, _gw(tmp_path))
    asyncio.run(c.curate(23))
    c.confirm(23, commit=False)

    item = store.load_foreshadow().get("F-009")
    assert item.status == "hinted"
    assert 23 in item.hinted_chs


def test_extract_date_does_not_swallow_verbs():
    """回归：正文「墙上刻着青云历1024年6月初一」曾抽出纪年「着青云」。

    断言纪年名不以常见动词/介词结尾，且能被 parse_fiction_date 解析。
    """
    from server.agents.curator import _extract_date
    from server.core.domain import parse_fiction_date

    cases = {
        "墙上刻着青云历1024年6月初一，笔画很浅。": "青云历",
        "他记得玄历990年腊月廿三那天下雨。": "玄历",
        "天元历88年三月十五。": "天元历",
    }
    for text, era in cases.items():
        got = _extract_date(text)
        assert got, f"未能抽出日期: {text}"
        assert got.startswith(era), f"纪年名错误: {got!r} 应形如 {era}"
        parsed = parse_fiction_date(got)
        assert parsed is not None, f"抽出的日期无法解析: {got!r}"
        assert parsed[0] == era


# ---------------------------------------------------- 复读检测口径（连续片段）

def test_shared_runs_ignores_scattered_short_collisions():
    """回归：8-gram 集合会把模板池造成的短碰撞报成「774 处重复」。

    真实复读是连续长片段；分散的短碰撞不该触发 high。
    """
    from server.agents.reviewer import _shared_runs

    a = "他在矿洞里走了很久，看见石壁上有刻痕，就伸手摸了摸，觉得很凉。" * 3
    b = "她在集市上挑了半天，看见摊子上有糖人，就掏钱买了一个，觉得挺甜。" * 3
    assert _shared_runs(a, b, min_len=30) == []

    big = "云尘把矿牌塞进怀里，转身走向岔道口，脚步很轻，怕惊动里面的人。" * 2
    assert _shared_runs(big, big, min_len=30), "整段照搬必须检出"


def test_reviewer_repetition_reports_contiguous_not_gram_count(store: NovelStore, tmp_path: Path):
    """跨章复读的 suggestion 必须说「连续重复 N 段／最长 M 字」，不是 8-gram 个数。"""
    block = "云尘把矿牌塞进怀里，转身走向岔道口，脚步很轻，怕惊动里面的人。"
    _ready_chapter(store, 31, block * 6)
    _ready_chapter(store, 32, block * 6)

    r = Reviewer(store, _gw(tmp_path))
    res = asyncio.run(r.review(32, use_model=False))
    rep = [i for i in res.report.issues if i.category == "repetition"]
    assert rep, "整段照搬必须被检出"
    assert "8-gram" not in rep[0].suggestion
    assert "连续" in rep[0].suggestion


def test_deconstructor_model_cannot_override_measured_stats(store: NovelStore, tmp_path: Path):
    """回归：模型报的 avg_words=2200 覆盖了实测 366，导致 pacing.md 与 structure.json 自相矛盾。

    统计口径（字数/密度/曲线）只能来自启发式实测；模型只补定性字段。
    """
    from server.agents.deconstructor import Deconstructor

    body = "\n\n".join(
        f"第{i}章 测试\n\n" + "他走上台阶，数到第十级。" * 40 for i in range(1, 6)
    )
    d = Deconstructor(store, _gw(tmp_path))
    res = asyncio.run(d.analyze(title="统计口径测试", content=body))

    import json

    base = store.p.root / "analysis" / "benchmarks" / "统计口径测试"
    structure = json.loads((base / "structure.json").read_text(encoding="utf-8"))
    pacing_md = (base / "pacing.md").read_text(encoding="utf-8")

    measured = structure["avg_words"]
    assert "2200" not in pacing_md, "模型编造的字数覆盖了实测值"
    assert f"平均章长：{measured} 字" in pacing_md, "pacing.md 与 structure.json 的 avg_words 必须一致"
    assert res.chapters == 5


def test_deconstructor_volume_structure_reflects_real_chapter_count(store: NovelStore, tmp_path: Path):
    """回归：10 章样本被模型写成「1 卷 60 章」，卷纲与实际章数矛盾。"""
    import json

    from server.agents.deconstructor import Deconstructor

    body = "\n\n".join(f"第{i}章 测试\n\n" + "他走上台阶。" * 50 for i in range(1, 6))
    asyncio.run(Deconstructor(store, _gw(tmp_path)).analyze(title="卷数测试", content=body))
    d = json.loads((store.p.root / "analysis/benchmarks/卷数测试/structure.json").read_text(encoding="utf-8"))
    assert sum(v["chapters"] for v in d["volumes"]) == d["chapters"] == 5


# ---------------------------------------------------- §8.5 索引增量更新

def test_confirm_updates_index_after_final(store: NovelStore, tmp_path: Path):
    """§8.5：章 final 后必须增量更新索引，否则新章检索不到。"""
    from server.retrieval.index import IndexBuilder

    _ready_chapter(store, 51, "矿灯照到尽头，是一堵不该存在的墙。" * 120)
    o = store.load_outline(51)
    o.title = "验收索引"
    o.foreshadow_actions = []
    store.save_outline(o)

    # 先建一次索引（模拟已有索引）
    IndexBuilder(store).build(full=True)
    before = IndexBuilder(store).stats().chapters

    c = Curator(store, _gw(tmp_path))
    asyncio.run(c.curate(51))
    res = c.confirm(51, commit=False)

    assert res["became_final"] is True
    assert res["index_update"]["updated"] is True
    after = IndexBuilder(store).stats().chapters
    assert after >= before, "索引章数不应减少"


def test_confirm_without_index_degrades_gracefully(store: NovelStore, tmp_path: Path):
    """索引不存在时，沉淀必须照样落盘（索引是可重建缓存，不能阻断）。"""
    _ready_chapter(store, 52, "他把矿牌按在石壁上。" * 120)
    store.save_outline(store.load_outline(52))

    c = Curator(store, _gw(tmp_path))
    asyncio.run(c.curate(52))
    res = c.confirm(52, commit=False)
    assert res["became_final"] is True
    assert res["index_update"]["updated"] is False
    assert any("索引" in w for w in res["warnings"])
    # 沉淀结果仍然生效
    assert store.load_outline(52).status == "final"


# ---------------------------------------------------- §9.3 避雷条目沉淀

def _fake_review(store: NovelStore, ch: int, categories: list[str], severity: str = "high") -> None:
    from server.core.schema import ReviewIssue, ReviewReport

    issues = [
        ReviewIssue(category=c, evidence=f"证据{ch}{i}", suggestion="改", severity=severity)
        for i, c in enumerate(categories)
    ]
    store.save_review(ReviewReport(ch=ch, issues=issues, model="test"))


def test_repeated_review_category_becomes_avoid_entry(store: NovelStore, tmp_path: Path):
    """§9.3：同一 category 反复出现 → 沉淀为 style-fingerprint 避雷条目。"""
    for ch in (61, 62, 63, 64, 65):
        _ready_chapter(store, ch, f"第{ch}章的正文内容。" * 120)
        store.save_outline(store.load_outline(ch))
        _fake_review(store, ch, ["ai-flavor"])

    c = Curator(store, _gw(tmp_path))
    asyncio.run(c.curate(65))
    res = c.confirm(65, commit=False)

    assert res["avoid_entries"], "连续 5 章都有 ai-flavor，应沉淀避雷条目"
    style = store.read_truth_text("style-fingerprint.md")
    assert "反复出现：ai-flavor" in style
    assert "反复出现的问题（自动沉淀）" in style


def test_single_occurrence_does_not_pollute_style(store: NovelStore, tmp_path: Path):
    """偶发一次不该沉淀：文风指纹不能被一次误报污染。"""
    _ready_chapter(store, 80, "只出现一次的问题。" * 120)
    store.save_outline(store.load_outline(80))
    _fake_review(store, 80, ["ai-flavor"])

    c = Curator(store, _gw(tmp_path))
    asyncio.run(c.curate(80))
    res = c.confirm(80, commit=False)

    assert res["avoid_entries"] == []
    assert "反复出现：ai-flavor" not in store.read_truth_text("style-fingerprint.md")


def test_avoid_entry_not_duplicated_on_second_run(store: NovelStore, tmp_path: Path):
    """同一 category 已沉淀过就不再重复追加。"""
    for ch in (71, 72, 73, 74, 75):
        _ready_chapter(store, ch, f"内容{ch}。" * 120)
        store.save_outline(store.load_outline(ch))
        _fake_review(store, ch, ["repetition"])

    c = Curator(store, _gw(tmp_path))
    asyncio.run(c.curate(75))
    c.confirm(75, commit=False)
    style1 = store.read_truth_text("style-fingerprint.md")
    assert "反复出现：repetition" in style1, "首次应沉淀"

    for ch in (76, 77):
        _ready_chapter(store, ch, f"内容{ch}。" * 120)
        store.save_outline(store.load_outline(ch))
        _fake_review(store, ch, ["repetition"])

    c2 = Curator(store, _gw(tmp_path))
    asyncio.run(c2.curate(77))
    res = c2.confirm(77, commit=False)
    style2 = store.read_truth_text("style-fingerprint.md")

    assert res["avoid_entries"] == [], "已存在的避雷条目不应重复追加"
    assert style1.count("反复出现：repetition") == style2.count("反复出现：repetition") == 1


def test_small_sample_does_not_trigger_avoid_entry(store: NovelStore, tmp_path: Path):
    """回归：4 章的书里 MIN_HITS=3 等于 75% 命中率，mock 的模板重复会污染真相源。

    阈值必须同时要求「次数够」和「占比够」，否则小样本一跑就误沉淀。
    """
    # 4 章里 3 章有 repetition：次数不足 5，不该沉淀
    for ch in (91, 92, 93):
        _ready_chapter(store, ch, f"正文{ch}。" * 120)
        store.save_outline(store.load_outline(ch))
        _fake_review(store, ch, ["repetition"])
    _ready_chapter(store, 94, "正文94。" * 120)
    store.save_outline(store.load_outline(94))
    _fake_review(store, 94, [])

    c = Curator(store, _gw(tmp_path))
    asyncio.run(c.curate(94))
    res = c.confirm(94, commit=False)

    assert res["avoid_entries"] == [], "3/4 章命中不该沉淀（样本太小）"
    assert "反复出现：repetition" not in store.read_truth_text("style-fingerprint.md")


def test_mid_severity_never_becomes_avoid_entry(store: NovelStore, tmp_path: Path):
    """避雷条目写进宪法会影响后续每章生成，只有 high 级问题值得上升。

    回归：mid 级的模板重复曾把「反复出现：repetition」写进文风指纹。
    """
    for ch in (101, 102, 103, 104, 105):
        _ready_chapter(store, ch, f"正文{ch}。" * 120)
        store.save_outline(store.load_outline(ch))
        _fake_review(store, ch, ["repetition"], severity="mid")

    c = Curator(store, _gw(tmp_path))
    asyncio.run(c.curate(105))
    res = c.confirm(105, commit=False)

    assert res["avoid_entries"] == [], "mid 级问题不该沉淀为避雷条目"
    assert "反复出现：repetition" not in store.read_truth_text("style-fingerprint.md")


# ---------------------------------------------------------------- §6.4 提案白名单


def test_proposal_whitelist_rejects_path_traversal():
    """§6.4「唯一写入口」是文档自称的**物理保证**，但白名单此前零测试。

    `_allowed` 只做字符串 startswith，不规范化路径：`memory/../evil.md`
    会通过白名单，而 `store.resolve()` 又把 `..` 折叠掉 → 实际写到书根的
    evil.md，**逃出了 memory/ 白名单目录**（不是逃出书根，但同样越权）。
    存储层挡不住这种「白名单逃逸」，所以白名单层必须自己规范化。
    """
    from server.agents.curator import _allowed

    # 必须放行：白名单内的正常目标
    assert _allowed("memory/ch-0001.summary.md")
    assert _allowed("truth/characters/mc.json")
    assert _allowed("truth/foreshadow.json")
    assert _allowed("outline/threads/main.md")

    # 必须拒绝：带 .. 的穿越（白名单逃逸）
    assert not _allowed("memory/../evil.md")
    assert not _allowed("memory/../../evil.md")
    assert not _allowed("truth/characters/../../evil.md")
    assert not _allowed("../evil.md")
    # 必须拒绝：白名单外的目录
    assert not _allowed("outline/chapters/ch-0001.json")
    assert not _allowed("chapters/ch-0001.md")
    # 必须拒绝：前缀擦边（memory 是白名单、memoryevil 不是）
    assert not _allowed("memoryevil.md")


def test_proposal_traversal_never_reaches_disk(store):
    """端到端兜底：即便白名单被绕过，`store.resolve` 也挡住逃出书根。"""
    from server.core.store import StoreError

    with pytest.raises(StoreError):
        store.resolve("memory/../../../etc/passwd")
def test_verify_evidence_only_checks_first_8_chars():
    """第 4 条「审稿必须带证据」的真实边界（文档表述偏强，此处钉住实际行为）。

    文档 PROJECT.md §4 说「evidence 不是正文里的字面子串就整条丢弃」。实测实现是
    reviewer._verify_evidence：只取 evidence 的前 8 个非空白字符做包含判定，且
    len(probe) < 2 直接豁免。所以「前 8 字真 + 后半编造」照样放行——这是有意的容错
    （模型常截断/加省略号），不是 bug，但文档写得比实现强，容易让人误判。
    将来实现改成完整比对，本测试会红，提醒回来改文档。
    """
    from server.agents.reviewer import _verify_evidence
    from server.core.schema import ReviewIssue

    body = "他握紧拳头走向门口，没有回头。院中的梧桐叶落了一地。"

    # 真幻觉：完全不在正文 -> 必须丢弃（这条是红线，绝不能放行）
    kept, rejected = _verify_evidence(
        [ReviewIssue(category="OOC", evidence="眼中闪过一丝狠厉", suggestion="x")], body
    )
    assert not kept and len(rejected) == 1, "完全编造的证据必须丢弃"

    # 前 8 字命中、后半编造 -> 当前放行（文档声称会丢弃，实测不会）
    half_fake = "他握紧拳头走向门口然后突然飞上了天"
    kept2, rejected2 = _verify_evidence(
        [ReviewIssue(category="OOC", evidence=half_fake, suggestion="x")], body
    )
    assert len(kept2) == 1, "前 8 字命中即放行（当前实现的容错边界）"
    assert half_fake not in body, "这条 evidence 整体确实不在正文里，放行即证明只查前缀"

    # 空 evidence -> 丢弃；单字 evidence -> 豁免放行
    _k, rej_empty = _verify_evidence([ReviewIssue(category="OOC", evidence="", suggestion="x")], body)
    assert len(rej_empty) == 1
    k_short, _r = _verify_evidence([ReviewIssue(category="OOC", evidence="的", suggestion="x")], body)
    assert len(k_short) == 1, "probe 长度 <2 时豁免"

    # 描述性类别（hook/words/pov）不受字面约束
    k_desc, _r2 = _verify_evidence([ReviewIssue(category="hook", evidence="章末缺乏钩子", suggestion="x")], body)
    assert len(k_desc) == 1, "非 LITERAL 类别可以是描述"



# ---------------------------------------------------------------- 缺陷 #73


def test_replan_never_rewinds_published(store: NovelStore, tmp_path: Path):
    """重跑规划不能把已发布章倒回草案。

    缺陷 #73：原先 planner.plan(save=True) 无条件 `o.status = "draft"`，
    published → draft 是非法迁移却没人拦——daily-panel 的存稿口径
    （stock = draft+reviewed+final，published 被排除）会凭空多一章，
    读者端已经看过的章节在作者端变回未发布。
    """
    p = Planner(store, _gw(tmp_path))
    for i, s in enumerate([
        "draft",
        "planned",
        "writing",
        "drafted",
        "reviewed",
        "final",
        "published",
    ], start=1):
        store.save_outline(
            ChapterOutline(ch=i, title=f"原题{i}", pov="mc", scenes=[Scene(at="开场", beat="起")], status=s)
        )
    before = {i: str(store.load_outline(i).status) for i in range(1, 8)}
    res = asyncio.run(p.plan(start_ch=1, count=7, save=True))
    after = {i: str(store.load_outline(i).status) for i in range(1, 8)}
    # 已发布章原样保留，且给出说明
    assert after[7] == "published", after
    assert before[7] == after[7]
    assert any("已发布" in w and "跳过覆盖" in w for w in res.warnings), res.warnings
    # reviewed / final 仍是老的跳过语义（先于 #73 就有）
    assert after[5] == "reviewed" and after[6] == "final", after
    # 在推进中的章不能被拉回 draft，只能降到 drafted
    for i in (2, 3, 4):
        assert after[i] == "drafted", f"第{i}章 {before[i]} -> {after[i]}"
    # 新章（原本是 draft）仍落 draft
    assert after[1] == "draft", after
    # rewritten 只记录真的变了状态的章
    assert {int(r["ch"]) for r in res.rewritten} == {2, 3}, res.rewritten  # ch4 本就是 drafted，没变


# ------------------------------------------------- 缺陷 #93：拆书失败不留残缺产物目录


def test_analyze_leaves_no_half_written_benchmark(tmp_path: Path):
    """`Deconstructor.analyze` 的四个产物是分批写的，中途失败不能留下残缺目录。

    修复前：新书第一次拆书时若第 2/3 个写入失败（磁盘满、权限、被占用），目录里只剩
    `structure.json` + `pacing.md`，而 `list_benchmarks` 只看 structure.json 存在就把它
    当成合法对标书列出来——作者会拿一本「只有半截产物」的书去做节奏对标。
    现在：本次新建的目录失败就整目录 rmtree；覆盖已有产物时保留旧文件（旧的是完整的）。
    """
    import asyncio
    from unittest import mock
    from server.agents import deconstructor as dm
    from server.agents.deconstructor import Deconstructor, list_benchmarks
    
    ws = tmp_path / "ws"
    st = Workbench(ws).create_novel(BookMeta(title="拆书回滚", platform="tomato"))
    text = "\n".join("第%d章 " % i + "正文内容" * 50 for i in range(1, 6))
    d = Deconstructor(st, None)

    # 先成功拆一本，作为「覆盖已有产物」的对照组
    asyncio.run(d.analyze(title="老书", content=text, use_model=False))
    old_dir = st.p.root / "analysis" / "benchmarks" / "老书"
    assert sorted(p.name for p in old_dir.iterdir()) == [
        "hooks.md",
        "pacing.md",
        "structure.json",
        "template.json",
    ]

    real = dm.atomic.write_text_atomic
    state = {"n": 0}

    def flaky(path, text_, **kw):
        state["n"] += 1
        if state["n"] == 3:
            raise OSError("模拟磁盘写满")
        return real(path, text_, **kw)

    with mock.patch.object(dm.atomic, "write_text_atomic", flaky):
        # 覆盖已有目录：失败后旧产物必须还在（不删旧目录）
        state["n"] = 0
        try:
            asyncio.run(d.analyze(title="老书", content=text, use_model=False))
            raise AssertionError("应当抛 OSError")
        except OSError:
            pass
        assert old_dir.exists()
        assert (old_dir / "hooks.md").exists()

        # 全新目录：失败后整个目录不该存在
        state["n"] = 0
        try:
            asyncio.run(d.analyze(title="新书炸", content=text, use_model=False))
            raise AssertionError("应当抛 OSError")
        except OSError:
            pass

    base = st.p.root / "analysis" / "benchmarks"
    assert not (base / "新书炸").exists()
    assert [b["id"] for b in list_benchmarks(st.p.root)] == ["老书"]


# ------------------------------------------------- 缺陷 #97：跨章复读检查 O(n^2)


def test_shared_runs_is_not_quadratic():
    """`_shared_runs` 是每次保存正文都要跑的跨章复读检查，不能是 O(n^2)。

    修复前：每个窗口做一次 `in clean_b`（O(n) 子串查找）再逐字扩展，整体 O(n^2)。
    实测两章各 5 万字 **1.6s**、10 万字（`MAX_CHAPTER_CHARS` 上限）**6.6s**。
    现在：b 的 min_len 窗口先做成集合（O(1) 查），命中后二分求最长延伸。
    新实现与旧实现做过 300 组随机对照 fuzz，输出逐条一致——提速不是靠改语义。
    """
    from server.agents.reviewer import _shared_runs
    import time

    # 语义不能变
    seg = "他握紧拳头走向门口没有回头院中的梧桐叶落了一地" * 3
    assert _shared_runs(seg, seg, min_len=30) == [seg.replace(" ", "")]
    assert _shared_runs("第一章完全不同的内容", "另一章也完全不一样", min_len=30) == []
    assert _shared_runs("短", "很短", min_len=30) == []
    assert _shared_runs("", "x" * 100, min_len=30) == []

    # 超长正文必须在百毫秒级完成
    big = "".join(chr(0x4E00 + (i % 2000)) for i in range(100_000))
    t0 = time.time()
    runs = _shared_runs(big, big, min_len=30)
    dt = time.time() - t0
    assert runs, "整段相同必须检出复读"
    assert dt < 1.0, f"10 万字跨章复读检查耗时 {dt:.2f}s，仍是 O(n^2)"


# 缺陷 #98：外部写坏的 yaml / 产物不能让服务起不来或整条 500
def test_yaml_top_level_must_be_mapping(tmp_path):
    """配置 yaml 顶层写成标量/列表时，必须给中文报错，不能静默回退。

    实测（修复前）：providers.yaml 写一句 "- a" → `raw.get` 抛 AttributeError，
    `AppContext.create` 直接崩，服务起不来；scheduler.yaml 同理抛 TypeError。
    静默回退到默认更糟——作者会以为「我配了夜审」，实际从没跑过。
    """
    import yaml

    from server.model.providers.registry import ProviderConfigError, build_registry, load_routing
    from server.stats.scheduler import load_scheduler_config

    cfg = tmp_path / "cfg"
    cfg.mkdir()

    # providers.yaml
    (cfg / "providers.yaml").write_text("- a\n- b\n", encoding="utf-8")
    with pytest.raises(ProviderConfigError) as ei:
        build_registry(cfg / "providers.yaml")
    assert "providers.yaml" in str(ei.value)

    (cfg / "providers.yaml").write_text("oops\n", encoding="utf-8")
    with pytest.raises(ProviderConfigError):
        build_registry(cfg / "providers.yaml")

    # routing.yaml（需要一个 registry 才能 load_routing）
    from server.model.providers.registry import DEFAULT_PROVIDERS

    reg = build_registry(cfg / "__none__.yaml")
    (cfg / "routing.yaml").write_text("- a\n- b\n", encoding="utf-8")
    with pytest.raises(ProviderConfigError) as ei:
        load_routing(cfg / "routing.yaml", reg)
    assert "routing.yaml" in str(ei.value)

    # scheduler.yaml
    (cfg / "scheduler.yaml").write_text("- a\n- b\n", encoding="utf-8")
    with pytest.raises(ValueError) as ei:
        load_scheduler_config(cfg)
    assert "scheduler.yaml" in str(ei.value)

    # 对照组：正常键值表照常工作
    ok = {"providers": [{"name": "p", "kind": "mock", "models": ["m"]}]}
    (cfg / "providers.yaml").write_text(yaml.safe_dump(ok), encoding="utf-8")
    assert build_registry(cfg / "providers.yaml") is not None

    (cfg / "scheduler.yaml").write_text(yaml.safe_dump({"enabled": True}), encoding="utf-8")
    assert load_scheduler_config(cfg)["enabled"] is True


def test_broken_benchmark_products_do_not_500(store, tmp_path):
    """analysis/ 里的产物被外部写成标量/列表时不能整条崩。

    实测（修复前）：structure.json 写 `"oops"` → `list_benchmarks` 抛
    `AttributeError: 'str' object has no attribute 'get'`，`GET /benchmarks` 500。
    """
    import json

    from server.agents.curator import Curator
    from server.agents.deconstructor import list_benchmarks

    root = store.p.root
    base = root / "analysis" / "benchmarks" / "坏书"
    base.mkdir(parents=True)

    # 注意：必须写成**合法 JSON**（带引号），否则 atomic.read_json 会直接返回
    # default，根本走不到 setdefault / .get 那一层——那样断言是假绿。
    for bad in ['"oops"', "[1, 2]", "42"]:
        (base / "structure.json").write_text(bad, encoding="utf-8")
        got = list_benchmarks(root)
        assert len(got) == 1, bad
        assert got[0]["id"] == "坏书"
        # 读不出 meta 就退回目录名，不能崩
        assert got[0]["title"] == "坏书"

    # 对照组：正常产物照常读出
    (base / "structure.json").write_text(json.dumps({"title": "正常", "chapters": 3}), encoding="utf-8")
    got = list_benchmarks(root)
    assert got[0]["title"] == "正常" and got[0]["chapters"] == 3

    # 提案包被写成标量：Curator.load_pack 不能抛 ValidationError
    (root / "memory").mkdir(exist_ok=True)
    (root / "memory" / "proposals-0001.json").write_text('"oops"', encoding="utf-8")
    assert Curator(store, None).load_pack(1) is None
    (root / "memory" / "proposals-0001.json").write_text("[1, 2]", encoding="utf-8")
    assert Curator(store, None).load_pack(1) is None

# ---------------- 缺陷 #100：空内容的 replace/create 不许清空真相源 ----------------


def test_replace_and_create_refuse_empty_content(store):
    """模型提案 {"op":"replace"} 不带 content 时，不能把真相源文件整段写空。

    修复前：`return prop.content or ""` 把缺省的 None 变成空串落盘，
    实测 foreshadow.json 被写成 0 字节，作者手上的伏笔台账凭空消失。
    要清空请用 op=delete —— 那是显式意图，空 content 不是。
    """
    root = store.p.root
    rel = "truth/foreshadow.json"
    p = root / rel
    orig = json.dumps({"items": [{"id": "F-001", "status": "planted", "desc": "剑"}]}, ensure_ascii=False)
    p.write_text(orig, encoding="utf-8")

    for op in ("replace", "create"):
        p.write_text(orig, encoding="utf-8")
        prop = Proposal(id="p1", file=rel, op=op)
        with pytest.raises(ValueError) as ei:
            store.apply_proposal(prop)
        assert "缺少 content" in str(ei.value), op
        # 关键：拒绝了还得保证盘上没被动过
        assert p.read_text(encoding="utf-8") == orig, op

        # 显式空串同样不许（content=""）
        prop = Proposal(id="p1", file=rel, op=op, content="")
        with pytest.raises(ValueError):
            store.apply_proposal(prop)
        assert p.read_text(encoding="utf-8") == orig

    # 对照组 1：有内容的 replace 照常覆盖
    prop = Proposal(id="p2", file=rel, op="replace", content=json.dumps({"items": []}, ensure_ascii=False))
    store.apply_proposal(prop)
    assert json.loads(p.read_text(encoding="utf-8")) == {"items": []}

    # 对照组 2：delete 才是合法的清空路径
    p.write_text(orig, encoding="utf-8")
    prop = Proposal(id="p3", file=rel, op="delete")
    store.apply_proposal(prop)
    assert not p.exists()

    # 对照组 3：patch 不受影响
    p.write_text(orig, encoding="utf-8")
    prop = Proposal(id="p4", file=rel, op="patch", patch={"version": 2})
    store.apply_proposal(prop)
    assert json.loads(p.read_text(encoding="utf-8"))["version"] == 2

# ---------------- 缺陷 #101：append 打 .json 会把真相源写成非 JSON ----------------


def test_append_on_json_truth_file_stays_valid(store):
    """`op=append` 打 `.json` 真相源时，不能掉进 md/txt 分支把正文追加在 JSON 后面。

    修复前：truth/foreshadow.json 变成 `{"items": [...]}` 后面再接一行中文，
    文件不再是合法 JSON，`load_foreshadow()` 直接退回空台账 —— 作者手上的伏笔全部消失，
    而提案本身返回的是成功。
    """
    root = store.p.root
    rel = "truth/foreshadow.json"
    p = root / rel
    orig = json.dumps(
        {"items": [{"id": "F-001", "status": "planted", "desc": "剑"}], "version": 1},
        ensure_ascii=False,
    )
    p.write_text(orig, encoding="utf-8")

    # 1) 带字典 patch 的 append：深合并，文件仍然是合法 JSON，伏笔不丢
    prop = Proposal(id="p1", file=rel, op="append", patch={"version": 2})
    store.apply_proposal(prop)
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["version"] == 2
    assert len(data["items"]) == 1
    assert len(store.load_foreshadow().items) == 1

    # 2) 只带 content 的 append：拒绝，且盘上原样不动
    p.write_text(orig, encoding="utf-8")
    prop = Proposal(id="p2", file=rel, op="append", content="补充：剑是家传的")
    with pytest.raises(ValueError) as ei:
        store.apply_proposal(prop)
    assert ".json" in str(ei.value)
    assert p.read_text(encoding="utf-8") == orig

    # 对照组：.jsonl 追加不受影响
    rel3 = "memory/entity-deltas.jsonl"
    p3 = root / rel3
    p3.parent.mkdir(parents=True, exist_ok=True)
    p3.write_text(json.dumps({"a": 1}) + "\n", encoding="utf-8")
    prop3 = Proposal(id="p3", file=rel3, op="append", lines=[json.dumps({"b": 2})])
    store.apply_proposal(prop3)
    assert json.loads(p3.read_text(encoding="utf-8").strip().split("\n")[-1]) == {"b": 2}

    # 对照组：.yaml 追加不受影响
    rel4 = "truth/timeline.yaml"
    p4 = root / rel4
    p4.write_text("- ch: 1\n  date: x\n", encoding="utf-8")
    prop4 = Proposal(id="p4", file=rel4, op="append", patch={"ch": 2, "date": "y", "event": "e"})
    store.apply_proposal(prop4)
    assert len(store.load_timeline()) == 2

# ---------------- 缺陷 #102：md 分节替换的两个静默写坏 ----------------


def test_replace_md_section_handles_fences_and_duplicates():
    """分节替换既要躲开围栏代码块里的 #，也要把同名分节全部替换。

    修复前：(a) 围栏代码块里的注释行被 SECTION_RE 当成标题，替换一个跟代码同名的
    分节会把围栏拦腰截断、文档写坏；(b) 同名标题出现两次时只替换第一个，
    第二个同名分节静默留在文档里 —— 作者以为改过了其实没改。
    """
    from server.core.store import replace_md_section

    fence = chr(96) * 3
    # (a) 围栏代码块里的 # 不能当标题
    md = "## 要点\n\n示例：\n\n" + fence + "\n## 这只是代码里的井号\n" + fence + "\n\n真正的结尾\n"
    bad = replace_md_section(md, "这只是代码里的井号", "注入")
    assert fence in bad, "围栏不能被删掉"
    assert fence + "\n## 这只是代码里的井号\n" + fence in bad, "围栏要整段原样保留"
    assert "真正的结尾" in bad, "被替换分节之外的正文要保留"
    # 追加到文末的新标题仍然要建出来
    assert "注入" in bad

    # (b) 同名分节必须全部替换
    dup = "## 避雷\n\n第一条\n\n## 其他\n\n x \n\n## 避雷\n\n第二条\n"
    out = replace_md_section(dup, "避雷", "新内容")
    assert out.count("新内容") == 2, f"两个同名分节都要替换：{out!r}"
    assert "第一条" not in out and "第二条" not in out
    assert "## 其他" in out, "中间的分节要保留"

    # 对照组：正常单分节替换语义不变
    one = "## 避雷\n\n旧的\n\n## 其他\n\n x \n"
    out1 = replace_md_section(one, "避雷", "新的")
    assert out1.startswith("## 避雷\n\n新的\n\n")
    assert "## 其他" in out1 and " x " in out1
    # 对照组：找不到标题时追加
    out2 = replace_md_section(one, "全新分节", "内容")
    assert out2.rstrip().endswith("## 全新分节\n\n内容")

# ---------------- 缺陷 #103：提案的删除面没有白名单 ----------------


def test_proposal_delete_is_restricted_to_truth_files(store):
    """`op=delete` 必须落在 curator 的真相源白名单里。

    修复前：`apply_proposal` 只靠 `resolve()` 挡「逃出书根」，书根内的东西随便删。
    实测 `op=delete` 打 book.json / chapters/ch-0001.md / outline/chapters/*.json 都直接删掉 ——
    book.json 一删这本书从书架消失，正文一删版本库里那份就成了唯一副本。
    """
    root = store.p.root
    (root / "chapters").mkdir(exist_ok=True)
    (root / "chapters" / "ch-0001.md").write_text("正文", encoding="utf-8")
    (root / "memory").mkdir(exist_ok=True)
    (root / "memory" / "ch-0001.summary.md").write_text("摘要", encoding="utf-8")
    (root / "outline" / "chapters").mkdir(parents=True, exist_ok=True)
    (root / "outline" / "chapters" / "ch-0001.json").write_text("{}", encoding="utf-8")

    # 越界的三处：必须拒绝，且盘上原样不动
    for rel in ("book.json", "chapters/ch-0001.md", "outline/chapters/ch-0001.json"):
        p = Proposal(id="d", file=rel, op="delete")
        with pytest.raises(StoreError):
            store.apply_proposal(p)
        assert (root / rel).exists(), f"{rel} 不该被删掉"

    # 对照组：白名单内的真相源照常可删
    rel = "memory/ch-0001.summary.md"
    store.apply_proposal(Proposal(id="ok", file=rel, op="delete"))
    assert not (root / rel).exists()

    # 对照组：白名单内的真相源照常可写
    rel2 = "truth/world-state.md"
    store.apply_proposal(Proposal(id="w", file=rel2, op="replace", content="# 世界状态\n\n改过。"))
    assert (root / rel2).read_text(encoding="utf-8").strip() != ""

# ---------------- 缺陷 #104：提案的写入面同样要过白名单 ----------------


def test_proposal_write_is_restricted_to_truth_files(store):
    """`op=replace/create/patch` 也要落在真相源白名单里。

    #103 只堵了删除。写入照样能改掉不该改的东西：实测 `op=replace` 打 book.json
    能把这本书的 title 改成任意值（书架索引被改），打 chapters/ch-0001.md 能把整章
    正文覆盖掉 —— 而作者改正文走的是 PUT /chapters，根本不经提案。
    PROJECT.md §2 承诺白名单是「物理保证」，它此前只挂在 Curator 的生成端，
    执行端不查就等于绕过生成端没人拦。
    """
    root = store.p.root
    (root / "chapters").mkdir(exist_ok=True)
    (root / "chapters" / "ch-0001.md").write_text("原正文abcdef", encoding="utf-8")
    before_title = json.loads((root / "book.json").read_text(encoding="utf-8"))["title"]

    # 越界写入：必须拒绝，且盘上原样不动
    for rel in ("book.json", "chapters/ch-0001.md", "outline/chapters/ch-0001.json"):
        with pytest.raises(StoreError):
            store.apply_proposal(
                Proposal(id="w", file=rel, op="replace", content="被覆盖的内容")
            )

    assert json.loads((root / "book.json").read_text(encoding="utf-8"))["title"] == before_title
    assert (root / "chapters" / "ch-0001.md").read_text(encoding="utf-8") == "原正文abcdef"

    # 对照组：白名单内的真相源照常可写
    rel = "truth/world-state.md"
    store.apply_proposal(Proposal(id="ok", file=rel, op="replace", content="# 世界状态\n\n改过。"))
    assert "改过" in (root / rel).read_text(encoding="utf-8")

    # 对照组：create 也在白名单内可用
    rel2 = "memory/ch-0002.summary.md"
    store.apply_proposal(Proposal(id="ok2", file=rel2, op="create", content="# 摘要\n第二幕。"))
    assert (root / rel2).exists()