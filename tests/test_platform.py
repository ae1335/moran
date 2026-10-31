"""platform / feedback / stats 层单测。"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from server.core.schema import BookMeta, ChapterOutline, PlatformProfile, Scene
from server.core.store import NovelStore, Workbench, count_cn_words
from server.feedback.ingest import FeedbackIngest, _classify_reviews
from server.model.base import RouteHop, RoutingTable
from server.model.gateway import ModelGateway
from server.model.providers.mock import MockProvider
from server.model.providers.registry import ProviderRegistry, ProviderSpec
from server.platform.exporter import Exporter
from server.platform.polish import AIPolish, REPLACE_RULES
from server.stats.writing import WritingStats


@pytest.fixture()
def store(tmp_path: Path) -> NovelStore:
    wb = Workbench(tmp_path / "ws")
    s = wb.create_novel(BookMeta(title="导出测试", genre="玄幻", platform="tomato"))
    return s


def _chapter(store: NovelStore, ch: int, body: str, title: str = "", hook: str = "突然，矿洞深处传来一声响。"):
    store.write_chapter(ch, f"# 第{ch}章\n\n{body}\n\n{hook}")
    store.save_outline(
        ChapterOutline(ch=ch, title=title or f"第{ch}章题", pov="mc", hook=hook, scenes=[Scene(goal="g")])
    )


# ---------------------------------------------------------------- 导出


def test_export_collection_creates_file_and_report(store: NovelStore):
    for i in (1, 2, 3):
        _chapter(store, i, "正文内容。" * 400)
    ex = Exporter(store)
    res = ex.export(mode="合集")
    assert res.chapters == 3
    assert res.files
    out = store.root / res.out_dir
    assert (out / res.report_path.split("/")[-1]).exists()
    txt = [f for f in res.files if f.endswith(".txt")]
    assert txt and (out / txt[0]).exists()
    content = (out / txt[0]).read_text(encoding="utf-8")
    assert "第1章" in content and "#" not in content  # markdown 已剥离


def test_export_split_mode(store: NovelStore):
    for i in (1, 2):
        _chapter(store, i, "正文。" * 500)
    ex = Exporter(store)
    res = ex.export(mode="分章")
    txts = [f for f in res.files if f.startswith("ch-")]
    assert len(txts) == 2


def test_export_precheck_flags_short_and_redline(store: NovelStore):
    _chapter(store, 1, "太短了。")
    _chapter(store, 2, "他决定自杀。" + "内容。" * 500)
    ex = Exporter(store)
    prof = store.load_profile("tomato")
    checks = ex.precheck([1, 2], prof)
    assert any(c.high_issues for c in checks if c.ch == 1)  # 字数不足
    assert any(i["category"] == "redline" for c in checks if c.ch == 2 for i in c.high_issues)


def test_export_report_lists_unresolved_high(store: NovelStore):
    _chapter(store, 1, "他自杀。" + "内容。" * 500)
    ex = Exporter(store)
    res = ex.export()
    report = store.root / res.report_path
    import json

    data = json.loads(report.read_text(encoding="utf-8"))
    assert data["unresolved_high"], "导出报告必须标注未处理的 high 问题"
    assert data["unresolved_high"][0]["ch"] == 1


def test_export_blocked_when_pipeline_disabled(store: NovelStore):
    _chapter(store, 1, "内容。" * 600)
    book = store.load_book()
    book.ai_flavor_pipeline = False
    store.save_book(book)
    ex = Exporter(store)
    res = ex.export()
    assert res.blocked and res.risk_ack_required
    # 显式风险确认后可导出
    res2 = ex.export(allow_risk=True)
    assert not res2.blocked


def test_export_title_length_check(store: NovelStore):
    _chapter(store, 1, "内容。" * 600, title="这是一个非常非常非常非常长的章节标题超过二十字限制")
    ex = Exporter(store)
    checks = ex.precheck([1], store.load_profile("tomato"))
    assert any(i["category"] == "title" for i in checks[0].mid_issues)


def test_export_includes_review_high_issues(store: NovelStore):
    from server.core.schema import ReviewIssue, ReviewReport

    _chapter(store, 1, "内容。" * 600)
    store.save_review(
        ReviewReport(ch=1, issues=[ReviewIssue(severity="high", category="OOC", evidence="x", suggestion="y")])
    )
    ex = Exporter(store)
    checks = ex.precheck([1], store.load_profile("tomato"))
    assert any(i["category"] == "OOC" for i in checks[0].high_issues)


# ---------------------------------------------------------------- 日更面板


def test_daily_panel_structure(store: NovelStore):
    for i in (1, 2, 3):
        _chapter(store, i, "内容。" * 600)
    ex = Exporter(store)
    panel = ex.daily_panel()
    for key in ("today_queue", "stock", "stock_days", "shortage_warning", "words_curve", "total_words"):
        assert key in panel
    assert panel["total_words"] > 0


def test_daily_panel_shortage_warning(store: NovelStore):
    _chapter(store, 1, "内容。" * 600)
    ex = Exporter(store)
    panel = ex.daily_panel()
    assert panel["shortage_warning"] is True  # 存稿 < 3 天


# ---------------------------------------------------------------- 反 AI 味


def test_polish_rule_layer_replaces_smells(store: NovelStore):
    _chapter(store, 1, "他仿佛看见了什么，不由得后退。眼中闪过一丝慌乱。")
    p = AIPolish(store)
    res = asyncio.run(p.polish(1, use_model=False))
    assert "仿佛" not in res.rewritten
    assert "不由得" not in res.rewritten
    assert res.issues
    assert res.diff.startswith("---")


def test_polish_apply_archives_version(store: NovelStore):
    _chapter(store, 1, "他仿佛看见了什么。" + "石壁很冷。" * 100)
    p = AIPolish(store)
    res = asyncio.run(p.polish(1, use_model=False, apply=True))
    assert res.applied
    assert "仿佛" not in store.read_chapter(1)
    assert store.list_versions(1), "改写前必须留档"


def test_polish_variance_detects_flat_rhythm(store: NovelStore):
    body = "他向前走了一步。" * 12
    _chapter(store, 1, body)
    p = AIPolish(store)
    res = asyncio.run(p.polish(1, use_model=False))
    assert any(i.kind == "节奏" for i in res.issues)


def test_polish_paraphrase_not_detected__known_limit(store: NovelStore):
    """§7 第 6 条已知限制：语义级同义改写检不出。

    两章讲的是同一件事，但逐句换了词序与用词——没有 10-gram 重合、没有 ≥30 字连续片段。
    这里**断言检不出**，把这个限制钉成可执行的事实：哪天真做了语义检测，
    这条测试会红，提醒你回来改文档 §7 第 6 条（而不是让文档和实现悄悄脱节）。
    """
    ch1 = "".join([
        "他握紧手中长剑，缓步走向那座古老的石殿。",
        "殿门半掩，门缝里渗出幽幽的青光。",
        "他深吸一口气，抬手推开沉重的石门。",
        "门后是一条向下的阶梯，深不见底。",
    ])
    ch2 = "".join([  # 逐句同义改写
        "手里的剑被他攥得更紧，他放慢脚步，朝那处年代久远的石头建筑走去。",
        "石门虚掩着，缝隙间透出淡淡的碧色微光。",
        "他长长地吸了一口气，伸手推开了那扇厚重的门扉。",
        "门内是一道通往地下的台阶，看不到尽头。",
    ])
    store.write_chapter(1, ch1)
    store.write_chapter(2, ch2)
    p = AIPolish(store)
    # 对照：字面照抄必须抓得到，否则说明检测器坏了、这条"测不出"没有意义
    store.write_chapter(2, ch1)
    assert any(i.kind == "复读" for i in p.repetition_check(ch1, 2)), "字面照抄应当被抓到"
    # 同义改写：当前实现检不出（已知限制，非缺陷）
    store.write_chapter(2, ch2)
    assert p.repetition_check(ch2, 2) == [], "语义级改写当前检不出（§7 第 6 条）；若已实现语义检测请更新文档"


def test_polish_repetition_across_chapters(store: NovelStore):
    same = "他抬头看向远处的山门，风雪正紧，钟声在山谷里回荡了很久很久。" * 12
    _chapter(store, 1, same)
    _chapter(store, 2, same)
    p = AIPolish(store)
    res = asyncio.run(p.polish(2, use_model=False))
    assert any(i.kind == "复读" for i in res.issues)


# ---------------------------------------------------------------- 反馈


def test_feedback_save_stats_and_load(store: NovelStore):
    fb = FeedbackIngest(store)
    r = fb.save_stats({"date": "2026-10-28", "reads": 1000, "chapters": {"1": {"drop_rate": 0.3}}})
    assert r["days"] == 1
    data = fb.load()
    assert data["days"][0]["reads"] == 1000


def test_feedback_save_review_classifies(store: NovelStore):
    fb = FeedbackIngest(store)
    r = fb.save_review(1, "太好看了，很爽\n水得不行，想弃\n精彩")
    assert r["summary"]["positive"] >= 2
    assert r["summary"]["negative"] >= 1


def test_feedback_top_drop_chapters(store: NovelStore):
    fb = FeedbackIngest(store)
    fb.save_stats(
        {
            "date": "2026-10-28",
            "chapters": {str(i): {"drop_rate": i / 100.0} for i in range(1, 101)},
        }
    )
    top = fb.top_drop_chapters(0.05)
    assert len(top) == 5
    assert 100 in top


def test_feedback_recheck_proposes_avoid_entries(store: NovelStore):
    _chapter(store, 1, "内容。" * 300)
    fb = FeedbackIngest(store)
    fb.save_stats({"date": "2026-10-28", "chapters": {"1": {"drop_rate": 0.9}}})
    res = fb.recheck()
    assert 1 in res.chapters
    assert res.findings
    assert res.proposals


def test_feedback_apply_writes_style_fingerprint(store: NovelStore):
    fb = FeedbackIngest(store)
    res = fb.recheck(chapters=[1])
    out = fb.apply_proposals(res.proposals or [{"entry": "- [第1章] 测试避雷"}])
    assert out["applied"] >= 1
    text = store.read_truth_text("style-fingerprint.md")
    assert "本章避雷" in text
    assert "测试避雷" in text or "第1章" in text


def test_classify_reviews_empty():
    assert _classify_reviews("")["total"] == 0


# ---------------------------------------------------------------- 统计


def test_writing_stats_overview(store: NovelStore):
    for i in (1, 2, 3):
        _chapter(store, i, "内容。" * 600)
    st = WritingStats(store)
    o = st.overview()
    assert o["chapters"] == 3
    assert o["total_words"] > 1000
    assert o["progress"]["target"] == 1200
    assert st.word_curve()


def test_writing_stats_daily_words_with_git(store: NovelStore):
    from server.core.gitbot import GitBot

    gb = GitBot(store.root)
    gb.init()
    _chapter(store, 1, "内容。" * 600)
    gb.commit_chapter(1, "开局", 1200)
    st = WritingStats(store)
    rows = st.daily_words()
    assert rows and rows[0]["words"] == 1200


def test_pacing_vs_benchmark_absent(store: NovelStore):
    st = WritingStats(store)
    r = st.pacing_vs_benchmark()
    assert r["available"] is False



# ------------------------------------------------- 缺陷 #75：已发布章正文不许直接改写


def test_polish_refuses_to_apply_on_published(store: NovelStore):
    """预览无害、落盘必须有 Writer 那道同款保护。

    实测（修复前）：章纲 status=published 时 `polish(apply=True)` 照样
    `APPLIED True` 且正文真的被改写——读者已看到的章节被悄悄改掉，
    而章纲还停在 published，daily-panel 口径对不上。
    """
    _chapter(store, 1, "他仿佛看见了什么，不由得后退。眼中闪过一丝慌乱。")
    o = store.load_outline(1)
    o.status = "published"
    store.save_outline(o)
    before = store.read_chapter(1)
    p = AIPolish(store)

    # 预览（apply=False）照常出报告，不受限
    prev = asyncio.run(p.polish(1, use_model=False, apply=False))
    assert prev.issues, "预览仍应报出 AI 味问题"
    assert prev.applied is False
    assert store.read_chapter(1) == before, "预览不能落盘"

    # 落盘必须被拒
    try:
        asyncio.run(p.polish(1, use_model=False, apply=True))
        raise AssertionError("已发布章的润色落盘应被拒绝")
    except ValueError as exc:
        assert "已发布" in str(exc), str(exc)
    assert store.read_chapter(1) == before, "被拒后正文必须原样"

    # 撤销发布后即可正常落盘（不误伤终态以外的章）
    o = store.load_outline(1)
    o.status = "final"
    store.save_outline(o)
    res = asyncio.run(p.polish(1, use_model=False, apply=True))
    assert res.applied


# ---------------------------------------------------------------- 缺陷 #123
# §9.2 承诺「发布前强制跑反 AI 味管线」，但 Exporter 到 #123 为止只查
# `book.ai_flavor_pipeline` 这个开关位（真值 True），从不真的扫一遍 ——
# 实测正文里带着 REPLACE_RULES 的套语照样导出、blocked=False、产物原样在。


def test_export_precheck_scans_ai_flavor_smells(store: NovelStore):
    """导出预检必须真的扫一遍套语，而不是只看开关位。

    修复前 Exporter.precheck 只查 `book.ai_flavor_pipeline` / `book.repetition_check`
    两个 bool：开关开着就放行，正文里的「仿佛整个世界」一条都没被检出。
    """
    smell = REPLACE_RULES[0][0]
    _chapter(store, 1, f"他{smell}，然后转身。" + "内容。" * 600)
    ex = Exporter(store, AIPolish(store))
    checks = ex.precheck([1], store.load_profile("tomato"))
    highs = checks[0].high_issues
    assert any(i["category"] == "ai-flavor" for i in highs), (
        f"套语没被扫出来：{highs}（缺陷 #123）"
    )
    hit = [i for i in highs if i["category"] == "ai-flavor"]
    assert smell in hit[0]["evidence"], hit[0]


def test_export_precheck_ai_flavor_respects_switches(store: NovelStore):
    """两个开关要各管各的：ai_flavor_pipeline 管规则层，repetition_check 管复读。

    开关名字骗人是 #123 的根因（只查位不跑管）。修好之后：关掉 ai_flavor_pipeline
    就没有套语检出；关掉 repetition_check 就没有跨章复读。
    """
    smell = REPLACE_RULES[0][0]
    _chapter(store, 1, f"他{smell}，然后转身。" + "内容。" * 600)
    book = store.load_book()
    book.ai_flavor_pipeline = False
    store.save_book(book)
    ex = Exporter(store, AIPolish(store))
    checks = ex.precheck([1], store.load_profile("tomato"))
    assert not any(i["category"] == "ai-flavor" for i in checks[0].high_issues), (
        "ai_flavor_pipeline 关掉了还检出套语 —— 开关没管住扫描范围（缺陷 #123）"
    )


def test_export_report_lists_ai_flavor_issues(store: NovelStore):
    """套语要进导出报告的 unresolved_high，作者才知道产物里还带着它。

    只报不改写：导出不该偷偷改正文（改写要作者点「反 AI 味润色」确认）。
    """
    import json

    smell = REPLACE_RULES[0][0]
    _chapter(store, 1, f"他{smell}，然后转身。" + "内容。" * 600)
    ex = Exporter(store, AIPolish(store))
    res = ex.export(mode="合集")
    data = json.loads((store.root / res.report_path).read_text(encoding="utf-8"))
    issues = data["unresolved_high"][0]["issues"]
    assert any(i["category"] == "ai-flavor" for i in issues), data["unresolved_high"]
    # 正文不许被导出流程偷偷改掉
    assert smell in store.read_chapter(1)


def test_polish_scan_is_read_only(store: NovelStore):
    """scan() 只读：既不改写正文，也不落盘版本。

    导出预检用它来算未处理项 —— 一旦它偷偷改正文，导出就变成了隐式润色。
    """
    smell = REPLACE_RULES[0][0]
    _chapter(store, 1, f"他{smell}，然后转身。" + "内容。" * 600)
    before = store.read_chapter(1)
    issues = AIPolish(store).scan(before, 1)
    assert any(i.kind == "替换" and i.original == smell for i in issues), issues
    assert store.read_chapter(1) == before, "scan() 改写了正文 —— 必须是纯只读"
