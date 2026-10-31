"""§11 发布导出 / §12 反馈闭环回归测试。"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from server.agents.planner import Planner
from server.core.domain import can_outline_transition
from server.core.schema import BookMeta, ChapterOutline, Scene
from server.core.store import NovelStore, Workbench
from server.feedback.ingest import FeedbackIngest
from server.platform.exporter import Exporter
from server.app import create_app


@pytest.fixture()
def client(tmp_path: Path):
    app = create_app(tmp_path / "ws")
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def store(tmp_path: Path) -> NovelStore:
    return Workbench(tmp_path / "ws").create_novel(
        BookMeta(title="商业闭环", genre="玄幻", platform="tomato")
    )


def _chapter(store: NovelStore, ch: int, status: str = "final") -> None:
    store.write_chapter(ch, f"# 第{ch}章\n\n" + "他继续向前。" * 300 + "\n\n突然，门后传来响声！")
    store.save_outline(
        ChapterOutline(
            ch=ch,
            title=f"第{ch}章标题",
            hook="门后是谁？",
            scenes=[Scene(goal="查探")],
            status=status,  # type: ignore[arg-type]
        )
    )


# ---------------------------------------------------------------- 发布状态


def test_published_transition_is_manual_and_reversible(store: NovelStore):
    _chapter(store, 1)
    assert can_outline_transition("final", "published")
    assert not can_outline_transition("drafted", "published")

    ex = Exporter(store)
    marked = ex.mark_published([1])
    assert marked["updated"] == [1]
    assert store.load_outline(1).status == "published"
    unmarked = ex.mark_published([1], published=False)
    assert unmarked["updated"] == [1]
    assert store.load_outline(1).status == "final"


def test_unfinished_chapter_cannot_be_marked_published(store: NovelStore):
    _chapter(store, 1, status="drafted")
    result = Exporter(store).mark_published([1])
    assert result["updated"] == []
    assert "只有定稿章" in result["skipped"][0]["reason"]


def test_published_chapter_is_not_counted_as_stock(store: NovelStore):
    _chapter(store, 1)
    _chapter(store, 2, status="drafted")
    ex = Exporter(store)
    before = ex.daily_panel()
    assert before["stock_breakdown"]["total"] == 2
    ex.mark_published([1])
    after = ex.daily_panel()
    assert after["stock_count"]["published"] == 1
    assert after["stock_count"]["final"] == 0
    assert after["stock_breakdown"]["total"] == 1
    assert after["stock"]["published"] == [1]


def test_today_queue_prioritizes_planned_over_draft(store: NovelStore):
    for ch, status in ((1, "draft"), (2, "planned"), (3, "draft"), (4, "planned")):
        _chapter(store, ch, status=status)
    queue = Exporter(store).daily_panel()["today_queue"]
    assert queue == [2, 4, 1, 3]


# ---------------------------------------------------------------- DOCX


def test_qidian_docx_export_is_valid_zip_and_xml(store: NovelStore):
    book = store.load_book()
    book.platform = "qidian"
    store.save_book(book)
    _chapter(store, 1)
    res = Exporter(store).export(platform="qidian", file_format="docx")
    docx_files = [f for f in res.files if f.endswith(".docx")]
    assert len(docx_files) == 1
    path = store.root / res.out_dir / docx_files[0]
    with zipfile.ZipFile(path) as zf:
        assert zf.testzip() is None
        document = zf.read("word/document.xml").decode("utf-8")
    assert "第1章标题" in document
    assert "他继续向前" in document
    report = json.loads((store.root / res.report_path).read_text(encoding="utf-8"))
    assert report["format"] == "docx"


def test_docx_export_rejected_for_tomato(store: NovelStore):
    _chapter(store, 1)
    with pytest.raises(ValueError, match="仅起点"):
        Exporter(store).export(platform="tomato", file_format="docx")


def test_docx_escapes_xml_special_characters(store: NovelStore):
    store.write_chapter(1, "# 第1章\n\nA & B <tag>。\n\n突然，危险来了！")
    store.save_outline(ChapterOutline(ch=1, title="A&B<测试>", scenes=[Scene(goal="g")], status="final"))  # type: ignore[arg-type]
    res = Exporter(store).export(platform="qidian", file_format="docx")
    name = next(f for f in res.files if f.endswith(".docx"))
    with zipfile.ZipFile(store.root / res.out_dir / name) as zf:
        document = zf.read("word/document.xml").decode("utf-8")
    assert "A &amp; B &lt;tag&gt;" in document
    assert "A&amp;B&lt;测试&gt;" in document


# ---------------------------------------------------------------- 反馈确认与 Planner


def test_feedback_apply_is_idempotent(store: NovelStore):
    _chapter(store, 1)
    fb = FeedbackIngest(store)
    res = fb.recheck(chapters=[1])
    first = fb.apply_proposals(res.proposals)
    second = fb.apply_proposals(res.proposals)
    assert first["applied"] >= 1
    assert second["applied"] == 0
    assert second["skipped_duplicates"] == first["applied"]
    text = store.read_truth_text("style-fingerprint.md")
    assert text.count(res.proposals[0]["entry"]) == 1


def test_feedback_proposal_ids_are_stable(store: NovelStore):
    _chapter(store, 1)
    fb = FeedbackIngest(store)
    a = fb.recheck(chapters=[1])
    b = fb.recheck(chapters=[1])
    assert [p["id"] for p in a.proposals] == [p["id"] for p in b.proposals]
    assert all(len(p["id"]) == 12 for p in a.proposals)


def test_fresh_review_is_used_when_supplied(store: NovelStore):
    _chapter(store, 1)

    class Issue:
        category = "hook"

    class Report:
        issues = [Issue()]

    res = FeedbackIngest(store).recheck(chapters=[1], reviews={1: Report()})
    assert res.findings[0]["review_source"] == "fresh"
    assert res.findings[0]["categories"]["hook"] == 1


def test_planner_extracts_avoidance_section_after_long_style(store: NovelStore):
    style = "# 文风\n" + ("很长的风格说明。" * 300) + "\n## 本章避雷\n- 第1章：避免章末平收\n\n## 其他\n别的不重要"
    brief = Planner._avoidance_brief(style)
    assert "避免章末平收" in brief
    assert "别的不重要" not in brief
    assert "反馈复盘避雷" in brief


def test_planner_ignores_empty_avoidance_placeholder():
    assert Planner._avoidance_brief("## 本章避雷\n（暂无。反馈复盘确认后自动追加。）") == ""


# ---------------------------------------------------------------- REST


def test_publish_and_feedback_confirm_rest(client: TestClient):
    book = client.post("/api/books", json={"title": "发布确认"}).json()["dir"]
    # 未走完整流程的 draft 章不能直接发布
    bad = client.post(f"/api/books/{book}/publish", json={"chapters": [1]})
    assert bad.status_code == 409

    # 反馈预览即使收到旧字段 apply=true 也不能直接写真相源
    client.post(
        f"/api/books/{book}/feedback/stats",
        json={"chapters": {"1": {"drop_rate": 0.8}}},
    )
    preview = client.post(f"/api/books/{book}/feedback/recheck", json={"apply": True})
    assert preview.status_code == 200
    assert preview.json()["applied"] == 0
    assert preview.json()["confirmation_required"] is True

    # 空正文时无提案；确认接口明确 404，而不是假报写入成功
    empty = client.post(
        f"/api/books/{book}/feedback/recheck/confirm",
        json={"accepted_ids": ["not-exists"]},
    )
    assert empty.status_code == 404


# ---------------------------------------------------------------- 缺陷 #70
# 网文是按章连载的：读者点进第 5 章时，第 1~4 章必须已经可读。
# 但 POST /publish {chapters:[5]} 原本返回 200 且一声不吭——作者端完全看不出来，
# 他自己的书架上 1~5 章都在。断层只在读者端成立。
def _finalize(store: NovelStore, n: int = 5) -> None:
    """把前 n 章都写成定稿（必须有 scenes + hook，否则章纲口径不完整）。"""
    for i in range(1, n + 1):
        store.write_chapter(i, "第%d章正文内容。" % i)
        store.save_outline(
            ChapterOutline(
                ch=i,
                title="第%d章" % i,
                status="final",
                scenes=[Scene(at="开场", beat="起")],
                hook="钩子",
            )
        )


def test_publish_out_of_order_reports_gap(store: NovelStore):
    """跳章发布必须在返回体里说出口，不能静默接受。"""
    _finalize(store)
    res = Exporter(store).mark_published([5], published=True)
    assert res["updated"] == [5], res
    assert res["gaps"] == [1, 2, 3, 4], res
    assert "读者会看到断层" in res["warning"], res
    assert store.load_outline(5).status == "published"


def test_publish_in_order_has_no_gap(store: NovelStore):
    """顺序发布不该报假警，否则告警会变成噪音被忽略。"""
    _finalize(store)
    res = Exporter(store).mark_published([1, 2, 3], published=True)
    assert res["gaps"] == [], res
    assert "warning" not in res, res
    res2 = Exporter(store).mark_published([5], published=True)
    assert res2["gaps"] == [4], res2


def test_unpublish_does_not_raise_false_gap(store: NovelStore):
    """撤销发布是把章放回存稿，不是制造断层。"""
    _finalize(store)
    Exporter(store).mark_published([1, 2, 3, 4, 5], published=True)
    res = Exporter(store).mark_published([5], published=False)
    assert res["gaps"] == [], res
    assert "warning" not in res, res
