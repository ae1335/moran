"""审稿 diff 预览单测（§13.2「问题清单逐条采纳/忽略、diff 预览」）。"""
from __future__ import annotations

from pathlib import Path

from server.core.schema import BookMeta, ReviewIssue, ReviewReport
from server.core.store import NovelStore, Workbench
from server.platform.fix_suggest import FixSuggester


def _book(tmp_path: Path) -> NovelStore:
    return Workbench(tmp_path).create_novel(BookMeta(title="diff 测试"))


def _ch(store: NovelStore, ch: int, text: str) -> None:
    store.write_chapter(ch, text)


def _review(store: NovelStore, ch: int, issues: list[ReviewIssue]) -> None:
    store.save_review(ReviewReport(ch=ch, issues=issues, model="test"))


# ---------------------------------------------------------------- 套语替换


def test_ai_flavor_saying_produces_diff(tmp_path: Path):
    st = _book(tmp_path)
    _ch(st, 1, "他抬头看了一眼，仿佛在等谁开口。")
    _review(st, 1, [ReviewIssue(category="ai-flavor", evidence="仿佛", suggestion="避免套语", severity="mid")])

    batch = FixSuggester(st).suggest(1)
    assert len(batch.suggestions) == 1
    s = batch.suggestions[0]
    assert s.applicable is True, s.reason
    assert "仿佛" in s.before
    assert "像在等谁开口" in s.after
    # diff 必须可读：-旧行 与 +新行 各占一行，不能拼在一起
    assert "\n-他抬头看了一眼，仿佛在等谁开口。" in s.diff
    assert "\n+他抬头看了一眼，像在等谁开口。" in s.diff
    assert "仿佛" not in batch.applied_text
    assert "像在等谁开口" in batch.applied_text


def test_long_saying_preferred_over_short(tmp_path: Path):
    """「眼中闪过一丝」不能被「眼中闪过」先命中，导致替换成半截。"""
    st = _book(tmp_path)
    _ch(st, 1, "他眼中闪过一丝慌乱，随即低下头。")
    _review(st, 1, [ReviewIssue(category="ai-flavor", evidence="眼中闪过一丝", severity="mid")])

    s = FixSuggester(st).suggest(1).suggestions[0]
    assert s.applicable
    assert "眼里掠过一点" in s.after
    assert "闪过一丝" not in s.after


def test_pure_deletion_saying(tmp_path: Path):
    st = _book(tmp_path)
    _ch(st, 1, "他不由得停住了脚。")
    _review(st, 1, [ReviewIssue(category="ai-flavor", evidence="不由得", severity="mid")])

    s = FixSuggester(st).suggest(1).suggestions[0]
    assert s.applicable
    assert s.after == "他停住了脚。", f"实际 {s.after!r}"


# ---------------------------------------------------------------- POV 删除


def test_pov_marker_sentence_removed(tmp_path: Path):
    st = _book(tmp_path)
    _ch(st, 1, "他推开门。他不知道的是，门外早有人等着。他握紧了拳。")
    _review(st, 1, [ReviewIssue(category="pov", evidence="他不知道的是", severity="mid")])

    s = FixSuggester(st).suggest(1).suggestions[0]
    assert s.applicable, s.reason
    assert "他不知道的是" not in s.after
    assert "他推开门" in s.after
    assert "他握紧了拳" in s.after


# ---------------------------------------------------------------- 不可自动改


def test_redline_not_auto_replaced(tmp_path: Path):
    st = _book(tmp_path)
    _ch(st, 1, "他点燃了一支烟。")
    _review(st, 1, [ReviewIssue(category="redline", evidence="烟", severity="high")])

    s = FixSuggester(st).suggest(1).suggestions[0]
    assert s.applicable is False
    assert "手动" in s.reason


def test_plot_issue_not_auto_replaced(tmp_path: Path):
    st = _book(tmp_path)
    _ch(st, 1, "他一步跨上了百丈高的崖顶。")
    _review(st, 1, [ReviewIssue(category="power", evidence="百丈高的崖顶", severity="high")])

    s = FixSuggester(st).suggest(1).suggestions[0]
    assert s.applicable is False
    assert "作者判断" in s.reason


def test_evidence_not_in_text_is_not_applicable(tmp_path: Path):
    """模型幻觉的 evidence 找不到 → 不产生 diff，但要给出原因。"""
    st = _book(tmp_path)
    _ch(st, 1, "他推开门，走了出去。")
    _review(st, 1, [ReviewIssue(category="ai-flavor", evidence="眼中闪过一丝", severity="high")])

    s = FixSuggester(st).suggest(1).suggestions[0]
    assert s.applicable is False
    assert "找不到" in s.reason


def test_no_evidence_is_not_applicable(tmp_path: Path):
    st = _book(tmp_path)
    _ch(st, 1, "正文内容。")
    _review(st, 1, [ReviewIssue(category="ai-flavor", evidence="", severity="mid")])

    s = FixSuggester(st).suggest(1).suggestions[0]
    assert s.applicable is False
    assert "没有给出正文片段" in s.reason


# ---------------------------------------------------------------- 选择性采纳


def test_only_selected_indices_produce_changes(tmp_path: Path):
    st = _book(tmp_path)
    _ch(st, 1, "他仿佛在等，宛如一尊石像。")
    _review(
        st,
        1,
        [
            ReviewIssue(category="ai-flavor", evidence="仿佛", severity="mid"),
            ReviewIssue(category="ai-flavor", evidence="宛如", severity="mid"),
        ],
    )

    batch = FixSuggester(st).suggest(1, issue_indices=[0])
    assert len(batch.suggestions) == 1
    assert "仿佛" in batch.suggestions[0].before
    assert "仿佛" not in batch.applied_text
    assert "宛如" in batch.applied_text, "未选中的问题不应被改"


def test_no_review_returns_empty_batch(tmp_path: Path):
    st = _book(tmp_path)
    _ch(st, 1, "正文。")
    batch = FixSuggester(st).suggest(1)
    assert batch.suggestions == []
    assert batch.as_dict()["changed"] is False


def test_truncated_evidence_still_locates(tmp_path: Path):
    """evidence 被截断成「他不知道的是」时，仍应定位到整句并删掉。"""
    st = _book(tmp_path)
    _ch(st, 1, "他站住了。他不知道的是，那扇门后已经没有人了。风还在吹。")
    _review(st, 1, [ReviewIssue(category="pov", evidence="他不知道的是…", severity="mid")])

    s = FixSuggester(st).suggest(1).suggestions[0]
    assert s.applicable, s.reason
    assert "没有了" not in s.after
    assert "风还在吹" in s.after


# ---------------------------------------------------------------- 不落盘


def test_suggest_does_not_write_files(tmp_path: Path):
    st = _book(tmp_path)
    text = "他仿佛在等。"
    _ch(st, 1, text)
    _review(st, 1, [ReviewIssue(category="ai-flavor", evidence="仿佛", severity="mid")])

    FixSuggester(st).suggest(1)
    assert st.read_chapter(1) == text, "预览不能改正文"


def test_structural_issue_reason_is_meaningful(tmp_path: Path):
    """回归：结构类问题（words/hook/title）没有字面 evidence，

    旧实现会先定位再报「evidence 在正文里找不到」，对作者毫无信息量。
    """
    st = _book(tmp_path)
    _ch(st, 1, "正文。" * 100)
    _review(
        st,
        1,
        [
            ReviewIssue(category="words", evidence="1200 字", severity="high"),
            ReviewIssue(category="hook", evidence="章末", severity="low"),
        ],
    )
    batch = FixSuggester(st).suggest(1)
    for s in batch.suggestions:
        assert s.applicable is False
        assert "找不到" not in s.reason, f"不该误报找不到：{s.reason}"
    reasons = {s.category: s.reason for s in batch.suggestions}
    assert "增删" in reasons["words"]
    assert "钩子" in reasons["hook"]


def test_repetition_can_delete_duplicate_sentence(tmp_path: Path):
    """复读类要给出最小可用动作：删掉章内重复句，而不是只说"请手动改"。"""
    st = _book(tmp_path)
    dup = "他握紧矿牌，向岔道口走去，脚步很轻。"
    _ch(st, 1, f"{dup}风停了。{dup}")
    _review(st, 1, [ReviewIssue(category="repetition", evidence=dup, severity="mid")])

    s = FixSuggester(st).suggest(1).suggestions[0]
    assert s.applicable, s.reason
    assert s.after.count("他握紧矿牌") == 1, f"应只留一次重复句，实际 {s.after!r}"


def test_cross_chapter_repetition_deletes_longest_sentence(tmp_path: Path):
    """跨章重复（章内无重复句）：给最小动作——删掉最长的句子，而不是「请手动改」。"""
    st = _book(tmp_path)
    _ch(st, 1, "他握紧矿牌，向岔道口走去，脚步很轻。风停了。远处传来钟声。")
    _review(st, 1, [ReviewIssue(category="repetition", evidence="他握紧矿牌", severity="mid")])

    s = FixSuggester(st).suggest(1).suggestions[0]
    assert s.applicable is True, s.reason
    assert "他握紧矿牌" not in s.after
    assert "风停了" in s.after
    assert "远处传来钟声" in s.after


def test_evidence_with_leading_punctuation_still_locates(tmp_path: Path):
    """回归：Reviewer 的 evidence 带前句句末标点（`。林澈在心里…`），

    而正文是 `\\n\\n林澈在心里…`，直接 find 失败 → 预览报「找不到」。
    """
    st = _book(tmp_path)
    body = (
        "有人在他背后说话，声音不大，刚好能让人听见。林澈没回头。\n\n"
        "林澈在心里把每个人的位置重新排了一遍，然后选了最坏的那个。\n\n"
        "渡口的缆绳绷得很紧。"
    )
    _ch(st, 1, body)
    _review(
        st,
        1,
        [ReviewIssue(category="repetition", evidence="。林澈在心里把每个人的位置重新排了一遍", severity="mid")],
    )

    s = FixSuggester(st).suggest(1).suggestions[0]
    assert s.before, "应能定位到段落，而不是报找不到"
    assert "找不到" not in s.reason


def test_repetition_deletes_evidence_sentence_not_longest(tmp_path: Path):
    """回归：删「最长句」会误删场景铺垫，留下真正复读的那句。

    evidence 指向哪句就删哪句，而不是按长度猜。
    """
    st = _book(tmp_path)
    body = "渡口的缆绳绷得很紧，船身贴着岸一沉一浮。林澈侧身让开半步。他侧过身。"
    _ch(st, 1, body)
    _review(
        st,
        1,
        [ReviewIssue(category="repetition", evidence="渡口的缆绳绷得很紧，船身贴着岸一沉一浮", severity="mid")],
    )

    s = FixSuggester(st).suggest(1).suggestions[0]
    assert s.applicable
    assert "渡口的缆绳" not in s.after, "应删掉 evidence 指向的那句"
    assert "林澈侧身让开半步" in s.after
