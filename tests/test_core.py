"""core 层单测：paths / atomic / store / domain / gitbot。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from server.core import atomic, domain
from server.core.paths import NovelPaths, ch_name, novel_dirname, safe_title
from server.core.schema import (
    BookMeta,
    ChapterOutline,
    CharacterCard,
    ForeshadowBook,
    ForeshadowItem,
    PowerEvent,
    Proposal,
    PowerScale,
    Thread,
    TimelineEntry,
)
from server.core.store import NovelStore, Workbench, count_cn_words
from server.core.gitbot import GitBot
from server.agents.naming import NameGenerator


# ---------------------------------------------------------------- paths


def test_ch_name_padding():
    assert ch_name(42) == "ch-0042"
    assert ch_name(7) == "ch-0007"
    assert ch_name(12345) == "ch-12345"


def test_novel_dirname_strips_illegal():
    assert novel_dirname("凡人修仙") == "novel-《凡人修仙》"
    assert "/" not in novel_dirname("a/b")
    assert ".." not in novel_dirname("a/../b")


def test_safe_title_empty():
    assert safe_title("   ") == "untitled"


def test_paths_layout(tmp_path: Path):
    p = NovelPaths(tmp_path)
    assert p.chapter_md(1).name == "ch-0001.md"
    assert p.outline_json(42).name == "ch-0042.json"
    assert p.summary_md(3).parent.name == "memory"
    assert p.rel(p.chapter_md(1)) == "chapters/ch-0001.md"


# ---------------------------------------------------------------- atomic


def test_atomic_write_and_read(tmp_path: Path):
    f = tmp_path / "sub" / "a.txt"
    atomic.write_text_atomic(f, "hello")
    assert atomic.read_text(f) == "hello"
    atomic.write_text_atomic(f, "world")
    assert atomic.read_text(f) == "world"
    # 不应残留 tmp 文件
    assert not list(f.parent.glob(".*.tmp"))


def test_atomic_json_roundtrip(tmp_path: Path):
    f = tmp_path / "x.json"
    atomic.write_json_atomic(f, {"书名": "测试", "n": [1, 2]})
    assert atomic.read_json(f)["书名"] == "测试"


def test_append_jsonl(tmp_path: Path):
    f = tmp_path / "a.jsonl"
    atomic.append_line_atomic(f, '{"a":1}')
    atomic.append_line_atomic(f, '{"a":2}')
    assert len(atomic.read_jsonl(f)) == 2


def test_read_missing_returns_default(tmp_path: Path):
    assert atomic.read_text(tmp_path / "nope.md") == ""
    assert atomic.read_json(tmp_path / "nope.json") is None


# ---------------------------------------------------------------- 建书


def test_create_novel_seeds_all_truth(tmp_path: Path):
    wb = Workbench(tmp_path)
    store = wb.create_novel(BookMeta(title="测试书", genre="玄幻"))
    p = store.p
    assert p.book_json.exists()
    assert p.constitution.exists()
    assert p.style_fingerprint.exists()
    assert p.world_file("power-system.md").exists()
    assert p.foreshadow.exists()
    assert p.timeline.exists()
    assert (p.characters_dir).is_dir()
    assert store.load_book().title == "测试书"
    # 平台 profile 自动生成
    prof = store.load_profile("tomato")
    assert prof.platform == "tomato"
    assert prof.hook_required is True


def test_shelf_refresh_finds_novel(tmp_path: Path):
    wb = Workbench(tmp_path)
    wb.create_novel(BookMeta(title="甲书"))
    wb.create_novel(BookMeta(title="乙书"))
    shelf = wb.refresh_shelf()
    assert len(shelf.novels) == 2
    assert {e.title for e in shelf.novels} == {"甲书", "乙书"}


def test_resolve_rejects_escape(tmp_path: Path):
    wb = Workbench(tmp_path)
    store = wb.create_novel(BookMeta(title="安全"))
    with pytest.raises(Exception):
        store.resolve("../../etc/passwd")


# ---------------------------------------------------------------- 角色卡


def test_character_roundtrip(tmp_path: Path):
    wb = Workbench(tmp_path)
    store = wb.create_novel(BookMeta(title="角色"))
    card = CharacterCard(
        id="mc",
        name="林澈",
        aliases=["小澈"],
        personality=["外冷内热"],
        boundaries=["绝不主动屠杀平民"],
        power_scale=PowerScale(rank="淬体七重", ceiling="淬体九重", forbidden="本卷内不得突破凝气期"),
    )
    store.save_character(card)
    got = store.get_character("mc")
    assert got is not None and got.name == "林澈"
    # 也支持按别名查
    assert store.get_character("小澈").id == "mc"
    assert store.character_map()["小澈"].name == "林澈"


# ---------------------------------------------------------------- 章纲与正文


def test_outline_roundtrip_and_char_refs(tmp_path: Path):
    wb = Workbench(tmp_path)
    store = wb.create_novel(BookMeta(title="章纲"))
    o = ChapterOutline(
        ch=42,
        title="淬骨",
        pov="mc",
        scenes=[
            {"loc": "world/geography#青岩矿脉", "chars": ["mc", "characters/elder-wang"], "goal": "夺矿"},
        ],
        must_include=["@characters/senior 首次登场"],
    )
    store.save_outline(o)
    got = store.load_outline(42)
    assert got.title == "淬骨"
    refs = got.char_refs()
    assert "mc" in refs and "elder-wang" in refs and "senior" in refs


def test_chapter_word_count():
    assert count_cn_words("你好世界") == 4
    assert count_cn_words("hello 世界") == 3  # 1 词 + 2 字
    assert count_cn_words("# 标题\n正文") == 4


def test_chapter_versions(tmp_path: Path):
    wb = Workbench(tmp_path)
    store = wb.create_novel(BookMeta(title="版本"))
    assert store.next_version_number(1) == 1
    store.write_version(1, 1, "第一版正文", {"model": "m1"})
    store.write_version(1, 2, "第二版正文更长一些", {"model": "m2"})
    vs = store.list_versions(1)
    assert len(vs) == 2
    assert vs[0]["version"] == 1 and vs[1]["model"] == "m2"
    assert store.next_version_number(1) == 3


# ---------------------------------------------------------------- 伏笔状态机


def test_foreshadow_transitions():
    fb = ForeshadowBook(items=[ForeshadowItem(id="F-001", desc="残图", planted_ch=3, status="planted", deadline_ch=120)])
    assert domain.apply_foreshadow_action(fb, "F-001", "hint", 42, "矿壁纹路") is None
    it = fb.get("F-001")
    assert it.status == "hinted" and it.hinted_chs == [42]
    assert domain.apply_foreshadow_action(fb, "F-001", "resolve", 121) is None
    assert fb.get("F-001").status == "resolved"
    assert fb.get("F-001").resolution_ch == 121
    # 已 resolved 不能再 drop
    issue = domain.apply_foreshadow_action(fb, "F-001", "drop", 122)
    assert issue is not None and issue.code == "illegal_transition"


def test_foreshadow_plant_new_creates_draft_then_planted():
    fb = ForeshadowBook(items=[])
    assert domain.apply_foreshadow_action(fb, "F-002", "plant", 5, "新伏笔") is None
    assert fb.get("F-002").status == "planted"
    assert fb.get("F-002").planted_ch == 5


def test_foreshadow_unknown_hint_is_error():
    fb = ForeshadowBook(items=[])
    issue = domain.apply_foreshadow_action(fb, "F-999", "hint", 10)
    assert issue is not None and issue.level == "error"


def test_foreshadow_overdue_and_active_selection():
    fb = ForeshadowBook(
        items=[
            ForeshadowItem(id="F-001", desc="超期", planted_ch=3, status="planted", deadline_ch=40),
            ForeshadowItem(id="F-002", desc="临近", planted_ch=3, status="hinted", deadline_ch=60, hinted_chs=[11]),
            ForeshadowItem(id="F-003", desc="远期", planted_ch=3, status="planted", deadline_ch=500),
            ForeshadowItem(id="F-004", desc="已回收", planted_ch=3, status="resolved", deadline_ch=10),
        ]
    )
    alerts = domain.overdue_alerts(fb, current_ch=42, horizon=30)
    ids = {a["id"] for a in alerts}
    assert "F-001" in ids and ids & {"F-002"} and "F-004" not in ids
    assert alerts[0]["level"] == "overdue"

    outline = ChapterOutline(ch=42, foreshadow_actions=[{"ref": "F-003", "action": "hint", "note": ""}])
    picked = {i.id for i in domain.active_for_context(fb, outline, 42, horizon=30)}
    assert "F-003" in picked  # 本章动作涉及
    assert "F-001" in picked  # 超期
    assert "F-004" not in picked  # 已结束


def test_next_foreshadow_id():
    fb = ForeshadowBook(items=[ForeshadowItem(id="F-007", desc="x")])
    assert fb.next_id() == "F-008"


# ---------------------------------------------------------------- 时间线


def test_parse_fiction_date_and_days():
    d = domain.parse_fiction_date("玄历1024年三月初七")
    assert d is not None
    assert d[1:] == (1024, 3, 7)
    assert domain.parse_fiction_date("乱七八糟") is None
    a = domain.date_to_days(domain.parse_fiction_date("玄历1024年3月1日"))
    b = domain.date_to_days(domain.parse_fiction_date("玄历1024年3月11日"))
    assert b - a == 10


def test_fiction_calendar_is_360_day_approximation():
    """已知限制（§7 第 7 条）钉死：虚构历法是 360 天/年、30 天/月的线性近似。

    这是设计取舍不是 bug——只用于「时间线单调 / 章节间隔」的相对校验，
    不能拿它算真实公历或农历日期差。将来接了真实历法，本测试会红，
    提醒回来改 §7 第 7 条。
    """

    def days(s):
        return domain.date_to_days(domain.parse_fiction_date(s))

    # 月差恒 30：真实公历 3→4 月是 31 天
    assert days("玄历1024年4月1日") - days("玄历1024年3月1日") == 30
    # 年差恒 360：真实是 365（闰年 366）
    assert days("玄历1025年1月1日") - days("玄历1024年1月1日") == 360
    # 纯算术、无月份长度表：13 月也照样接
    assert domain.parse_fiction_date("玄历1024年13月1日") is not None
    # 古历写法靠字面映射（腊月→12、廿→20），不是按真实农历置闰推算
    assert domain.parse_fiction_date("玄历1024年腊月廿三") == ("玄历", 1024, 12, 23)
    # 缺年号则完全不认：不做任何真实农历补全
    assert domain.parse_fiction_date("腊月廿三") is None


def test_validate_timeline_detects_reversal_and_mismatch():
    entries = [
        TimelineEntry(ch=3, date="玄历1024年三月初七", event="入门", days_since_start=0),
        TimelineEntry(ch=42, date="玄历1024年四月初二", event="冲突", days_since_start=26),
        TimelineEntry(ch=50, date="玄历1024年三月十五", event="倒流", days_since_start=40),
    ]
    issues = domain.validate_timeline(entries)
    codes = {i.code for i in issues}
    assert "time_reversed" in codes


def test_normalize_timeline_backfills_days():
    entries = [
        TimelineEntry(ch=3, date="玄历1024年三月初七"),
        TimelineEntry(ch=42, date="玄历1024年四月初二"),
    ]
    out = domain.normalize_timeline(entries)
    assert out[0].days_since_start == 0
    assert out[1].days_since_start == 25  # 三月初七 → 四月初二 = (4-3)*30 + (2-7) = 25


# ---------------------------------------------------------------- 战力


def test_validate_power_events_forbidden():
    chars = {
        "mc": CharacterCard(
            id="mc", name="林澈", power_scale=PowerScale(rank="淬体七重", forbidden="本卷内不得突破凝气期")
        )
    }
    outline = ChapterOutline(ch=42, power_events=[PowerEvent(char="mc", to="凝气期")])
    issues = domain.validate_power_events(outline, chars)
    assert any(i.level == "error" for i in issues)


def test_validate_power_events_ok():
    chars = {"mc": CharacterCard(id="mc", name="林澈", power_scale=PowerScale(forbidden="凝气期"))}
    outline = ChapterOutline(ch=42, power_events=[PowerEvent(char="mc", to="淬体八重")])
    assert domain.validate_power_events(outline, chars) == []


# ---------------------------------------------------------------- 提案落盘


def test_proposal_create_and_patch_json(tmp_path: Path):
    wb = Workbench(tmp_path)
    store = wb.create_novel(BookMeta(title="提案"))
    # create md
    diff = store.apply_proposal(Proposal(file="memory/ch-0001.summary.md", op="create", content="# 摘要\n主角入门。"))
    assert "摘要" in diff
    assert store.read_summary(1).startswith("# 摘要")

    # patch json（深合并）
    store.save_foreshadow(ForeshadowBook(items=[ForeshadowItem(id="F-001", desc="残图", planted_ch=3, status="planted")]))
    # §6.4 规格原文形态：{"F-001": {"status": "hinted", "hinted_chs": [42]}}
    store.apply_proposal(
        Proposal(file="truth/foreshadow.json", op="patch", patch={"F-001": {"status": "hinted", "hinted_chs": [42]}})
    )
    fb = store.load_foreshadow()
    assert fb.get("F-001").status == "hinted"
    assert fb.get("F-001").hinted_chs == [42]
    assert fb.get("F-001").desc == "残图"  # 其它字段不被冲掉


def test_proposal_patch_md_section(tmp_path: Path):
    wb = Workbench(tmp_path)
    store = wb.create_novel(BookMeta(title="分节"))
    store.apply_proposal(Proposal(file="truth/world-state.md", op="patch", section="主角处境", content="淬体七重，外门弟子。"))
    text = store.read_truth_text("world-state.md")
    assert "淬体七重" in text
    # 再次替换同节，不重复追加
    store.apply_proposal(Proposal(file="truth/world-state.md", op="patch", section="主角处境", content="淬体八重。"))
    text = store.read_truth_text("world-state.md")
    assert "淬体八重" in text and "淬体七重" not in text
    assert text.count("## 主角处境") == 1


def test_proposal_append_jsonl(tmp_path: Path):
    wb = Workbench(tmp_path)
    store = wb.create_novel(BookMeta(title="追加"))
    store.apply_proposal(Proposal(file="memory/entity-deltas.jsonl", op="append", lines=['{"ch":1,"entity":"mc"}']))
    store.apply_proposal(Proposal(file="memory/entity-deltas.jsonl", op="append", lines=['{"ch":2,"entity":"mc"}']))
    rows = atomic.read_jsonl(store.p.entity_deltas)
    assert len(rows) == 2


def test_preview_proposal_does_not_write(tmp_path: Path):
    wb = Workbench(tmp_path)
    store = wb.create_novel(BookMeta(title="预览"))
    before = store.read_truth_text("world-state.md")
    store.preview_proposal(Proposal(file="truth/world-state.md", op="patch", section="主角处境", content="X"))
    assert store.read_truth_text("world-state.md") == before


def test_proposal_append_yaml_list(tmp_path: Path):
    wb = Workbench(tmp_path)
    store = wb.create_novel(BookMeta(title="时间线"))
    store.apply_proposal(
        Proposal(file="truth/timeline.yaml", op="append", patch={"ch": 3, "date": "玄历1024年三月初七", "event": "入门"})
    )
    entries = store.load_timeline()
    assert len(entries) == 1 and entries[0].event == "入门"


# ---------------------------------------------------------------- git


def test_gitbot_commit_and_tag(tmp_path: Path):
    wb = Workbench(tmp_path)
    store = wb.create_novel(BookMeta(title="git书"))
    gb = GitBot(store.root)
    r = gb.init()
    assert r.ok
    store.write_chapter(1, "正文内容" * 100)
    c = gb.commit_chapter(1, "开局", 500)
    assert c.ok and c.sha
    assert "ch/0001" in gb.tags()
    log = gb.log()
    assert log and "ch-0001" in log[0]["message"]
    # 无变更时是 noop
    c2 = gb.commit_all("空提交")
    assert c2.ok and c2.message == "noop"
    # 时光机：读历史版本
    assert gb.show_file_at("ch/0001", "chapters/ch-0001.md") is not None
    hist = gb.file_history("chapters/ch-0001.md")
    assert len(hist) >= 1


def test_gitbot_ignores_rebuildable(tmp_path: Path):
    wb = Workbench(tmp_path)
    store = wb.create_novel(BookMeta(title="ignore书"))
    gb = GitBot(store.root)
    gb.init()
    (store.p.rag_index).mkdir(parents=True, exist_ok=True)
    (store.p.rag_index / "x.db").write_text("binary")
    gb.commit_all("init")
    assert gb.show_file_at("HEAD", ".rag-index/x.db") is None


def test_cn_num_supports_archaic_day_forms():
    """廿/卅 是网文古历常用写法，解析不了会让整条时间线校验失准。"""
    from server.core.domain import cn_num, parse_fiction_date

    assert cn_num("廿三") == 23
    assert cn_num("廿") == 20
    assert cn_num("卅") == 30
    assert cn_num("初七") == 7
    assert cn_num("十二") == 12
    assert parse_fiction_date("青云历1024年八月初二") == ("青云历", 1024, 8, 2)
    assert parse_fiction_date("玄历990年腊月廿三") == ("玄历", 990, 12, 23)


def test_cli_has_timeline_and_foreshadow_commands(tmp_path):
    """M1 要求不依赖 Web 也能闭环：时间线与伏笔必须在 CLI 可查。"""
    from server.cli import build_parser

    p = build_parser()
    # argparse 子命令名可从 _subparsers 里取
    choices = set()
    for action in p._actions:
        if getattr(action, "choices", None) and isinstance(action.choices, dict):
            choices |= set(action.choices)
    assert {"timeline", "foreshadow"} <= choices


def test_serve_defaults_to_loopback_only():
    """§7 第 3 条安全声明：单机单用户，只应绑 127.0.0.1。

    这条测试锁的是**默认值**。文档说「只应绑 127.0.0.1」，但此前没有任何测试看着它——
    哪天有人把 default 改成 0.0.0.0，全套测试仍全绿，而服务已经暴露到局域网。
    声明与实现分离就是隐患，所以把声明钉成可执行的事实。
    """
    from server.cli import build_parser

    p = build_parser()
    args = p.parse_args(["serve"])
    assert args.host == "127.0.0.1", f"serve 默认 host 必须是 127.0.0.1，实际 {args.host}"
    # 显式指定时仍允许改（运维自己承担暴露风险），这里只锁默认行为
    assert p.parse_args(["serve", "--host", "0.0.0.0"]).host == "0.0.0.0"


def test_daily_panel_counts_final_as_stock(tmp_path):
    """回归：定稿未发布的章必须计入存稿，否则面板显示「定稿箱 8 / 存稿 0 天」自相矛盾。"""
    from server.core.schema import BookMeta, ChapterOutline
    from server.platform.exporter import Exporter

    wb = Workbench(tmp_path)
    st = wb.create_novel(BookMeta(title="存稿测试"))
    for ch in (1, 2, 3, 4):
        st.write_chapter(ch, "他握紧矿牌。" * 200)
        o = ChapterOutline(ch=ch, title=f"章{ch}", hook="钟响。", status="final")
        st.save_outline(o)

    panel = Exporter(st).daily_panel()
    assert panel["stock_breakdown"]["final_unpublished"] == 4
    assert panel["stock_breakdown"]["total"] == 4
    assert panel["stock_days"] == round(4 / max(1, panel["daily_target"]), 1)
    assert panel["stock_days"] > 0, "写完的章必须算存稿"


def test_daily_panel_words_curve_not_word_curve(tmp_path):
    """字段名契约：前端 DailyView 读的是 words_curve，改名会静默变成空图表。"""
    from server.core.schema import BookMeta
    from server.platform.exporter import Exporter

    st = Workbench(tmp_path).create_novel(BookMeta(title="曲线测试"))
    st.write_chapter(1, "他走了。" * 300)
    panel = Exporter(st).daily_panel()
    assert "words_curve" in panel
    assert "word_curve" not in panel
    assert panel["words_curve"] and panel["words_curve"][0]["ch"] == 1


def test_export_strips_duplicate_chapter_title(tmp_path):
    """回归：导出文件曾出现「第1章 淬骨」连写两遍（正文 md 自带标题）。"""
    from server.core.schema import BookMeta, ChapterOutline
    from server.platform.exporter import Exporter

    wb = Workbench(tmp_path)
    st = wb.create_novel(BookMeta(title="导出测试"))
    st.write_chapter(1, "# 第1章 淬骨\n\n矿灯的火苗歪向一侧，说明前面有风。\n")
    st.save_outline(ChapterOutline(ch=1, title="淬骨", hook="钟响。", status="final"))

    res = Exporter(st).export(platform="tomato", chapters=[1], mode="合集")
    text = (st.p.root / res.out_dir / res.files[0]).read_text(encoding="utf-8")
    assert text.count("第1章 淬骨") == 1, f"标题重复了：\n{text[:120]}"
    assert "矿灯的火苗" in text
def test_git_failure_returns_commit_result_not_raise(tmp_path, monkeypatch):
    """第 6 条「失败不静默降级」的 git 分支：失败要返回 CommitResult(ok=False)，绝不抛。

    此前这条分支零测试（grep CommitResult 在 tests/ 下无命中）。它守的是「正文已写好但
    git 挂了」这个真实场景——一旦改成抛异常，写章流程会整段崩掉，正文跟着丢。
    """
    from server.core.gitbot import GitBot

    bot = GitBot(tmp_path)
    monkeypatch.setattr(type(bot), "_ensure", lambda self: (_ for _ in ()).throw(RuntimeError("git 炸了")))
    res = bot.commit_all("测试：失败不抛")
    assert res.ok is False, "git 失败必须体现在 ok=False 上"
    assert "git 炸了" in (res.error or ""), f"错误信息要透传，实际: {res.error!r}"
    assert res.sha == "", "失败不应伪造 sha"

    # 正文不受影响：调用方拿到的仍是结果对象而不是异常
    assert isinstance(res.error, str) and res.error



def test_store_rejects_book_dirname_traversal(tmp_path: Path):
    """书目录名必须是单段名（纵深防御）。

    「以磁盘为准」的查找原本不拒越界目录名：只要是 novels/../../outside 且里面
    摆了 book.json 就能被打开。当前 API 层碰不到（Starlette 先折叠路径，编码
    形式 %2e%2e 会被 404 挡在路由外），但守卫不能依赖调用方的框架行为。
    守在 get_store：find_store 的 key 可以是书名（书名含 / 是合法的），两条
    分支最终都经过 get_store。
    """
    from server.api.deps import AppContext
    from server.core.schema import BookMeta
    from server.core.store import StoreError

    ws = tmp_path / "ws"
    ctx = AppContext.create(ws)
    ctx.workbench.create_novel(BookMeta(title="正文", platform="tomato"))
    # ws 外面摆一本内容完整的假书（有 book.json + 章节）
    out = ws.parent / "outside"
    (out / "chapters").mkdir(parents=True, exist_ok=True)
    (out / "book.json").write_text(
        '{"title":"越界书","platform":"tomato"}', encoding="utf-8"
    )

    for key in ["../../outside", "..", "."]:
        with pytest.raises(StoreError, match="非法书目录名"):
            ctx.workbench.get_store(key)
        # find_store 最终也要么被守住院、要么查不到，绝不返回越界 store
        with pytest.raises(StoreError):
            st = ctx.workbench.find_store(key)
            assert "outside" not in st.p.root.name

    # 正常单段目录名不受影响
    d = ctx.workbench.refresh_shelf().novels[0].dir
    assert ctx.workbench.get_store(d).p.root.name == d
    assert ctx.workbench.find_store(d).p.root.name == d
    # 按书名查找也不受影响（书名可以含斜杠，目录名才被守）
    assert ctx.workbench.find_store("正文").p.root.name == d

def test_truth_source_writes_are_atomic(tmp_path):
    """缺陷 #62：真相源写入必须走 atomic（写到 tmp 再 rename）。

    直接 write_text 的话，写到一半崩掉/被杀，留下的就是半截文件——而这本书的
    「文件即真相源」铁律（PROJECT.md §1）意味着半截文件会被当成真相读进来。
    """
    import inspect

    from server.core import atomic
    from server.core.store import NovelStore

    src = inspect.getsource(NovelStore)
    # store 里所有落到真相源的写入点都必须以 atomic. 开头
    for name in ("save_book", "save_outline", "save_review", "write_chapter"):
        body = inspect.getsource(getattr(NovelStore, name))
        assert "atomic." in body, f"{name} 没有走 atomic 写入"
    # 全仓 server/ 下不许再出现 元信息/正文 的裸 write_text（.meta.json 与 style-fingerprint 已修）
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent / "server"
    bad = []
    for f in root.rglob("*.py"):
        txt = f.read_text(encoding="utf-8")
        for i, line in enumerate(txt.splitlines(), 1):
            if ".write_text(" in line and "atomic.write_text_atomic(" not in line:
                # 允许：init 脚手架（registry 写 env 模板 / gitignore）与导出产物（exporter 写导出目录）。
                # 这两类都不是真相源——它们是「一次性生成物」，半截了重跑就行。
                if "registry.py" in str(f) or "exporter.py" in str(f):
                    continue
                bad.append(f"{f.relative_to(root)}:{i}: {line.strip()}")

    assert not bad, "发现非原子写入：" + "; ".join(bad)
    # 原子写本身：断电/中断也不会留下半截（tmp -> replace）
    p = tmp_path / "a.md"
    atomic.write_text_atomic(p, "完整内容")
    assert p.read_text(encoding="utf-8") == "完整内容"
    assert not list(tmp_path.glob("*.tmp")), "原子写不应留下 tmp 残留"


def test_atomic_write_cleans_up_tmp_on_failure(tmp_path, monkeypatch):
    """缺陷 #63：原子写失败时必须清掉 tmp 文件。

    rename 那一步失败而 tmp 不清理的话，每次失败都留一个 .tmp 垃圾文件在目录里；
    积累起来目录会越来越脏，而且外部编辑器会把 .tmp 当候选正文挑出来。
    """
    import os

    from server.core import atomic

    p = tmp_path / "x.md"
    real_replace = os.replace

    def boom_replace(a, b, **k):
        raise OSError("rename failed")

    monkeypatch.setattr(os, "replace", boom_replace)
    with pytest.raises(OSError):
        atomic.write_text_atomic(p, "内容")
    monkeypatch.setattr(os, "replace", real_replace)
    # 目标文件绝不能被创建（半截真相源）
    assert not p.exists(), "写入失败却留下了目标文件（半截真相源）"
    # tmp 也不能残留
    left = [q for q in tmp_path.iterdir()]
    assert not left, f"原子写失败留下了 tmp 残留：{[q.name for q in left]}"

    # 正常路径：tmp 不残留且内容完整
    atomic.write_text_atomic(p, "真的内容")
    assert p.read_text(encoding="utf-8") == "真的内容"
    assert len([q for q in tmp_path.iterdir()]) == 1, "正常写入后目录里应只剩目标文件"



def test_atomic_readers_survive_half_written_files(tmp_path):
    """缺陷 #64：读路径必须能扛住半截文件。

    原子写保证了「要么旧要么新」，但外部编辑器/同步盘/git 操作仍可能在目录里
    留下写了一半的 json。三个 read_* 遇到坏文件必须走 default，
    不能让整个 API 500——书页打不开比内容少两条更糟。
    """
    from server.core import atomic

    # 半截 json（截断在中间）
    broken = tmp_path / "b.json"
    broken.write_text('{"a": 1, "b": [2, 3', encoding="utf-8")
    assert atomic.read_json(broken, default="FALLBACK") == "FALLBACK"
    # 半截 yaml（非法缩进）
    by = tmp_path / "b.yaml"
    by.write_text("a:\n  - 1\n b: [unclosed\n", encoding="utf-8")
    assert atomic.read_yaml(by, default="FALLBACK") == "FALLBACK"
    # 空文件 / 纯空白
    empty = tmp_path / "e.json"
    empty.write_text("", encoding="utf-8")
    assert atomic.read_json(empty, default=42) == 42
    # jsonl 里混进坏行：坏行跳过，好行照读
    jl = tmp_path / "x.jsonl"
    jl.write_text('{"a": 1}\n{"b": 2' + "\n" + '{"c": 3}\n', encoding="utf-8")
    rows = atomic.read_jsonl(jl)
    assert rows == [{"a": 1}, {"c": 3}], rows
    # 不存在的文件也走 default（不抛）
    assert atomic.read_text(tmp_path / "nope.md", default="D") == "D"
    assert atomic.read_json(tmp_path / "nope.json", default=None) is None


# ---------------------------------------------------------------- 缺陷 #72/#73 CLI 分支


def test_cli_confirm_refuses_to_rewind_terminal_states(tmp_path):
    """CLI 的 `moran confirm` 也要守状态机。

    缺陷 #72 修完 HTTP 与 planner 两处后，CLI 的 cmd_confirm 仍是无条件
    `o.status = "planned"`——同一条命令，走 Web 被 409 拒绝，走终端却能
    把已发布章拉回 planned。现在它复用 planner.confirm_outline。
    """
    from server.core.store import Workbench
    from server.core.schema import BookMeta, ChapterOutline, Scene
    from server.cli import cmd_confirm

    ws = tmp_path / "ws"
    st = Workbench(ws).create_novel(BookMeta(title="CLI确认", platform="tomato"))
    for i, s in enumerate(
        ["draft", "planned", "writing", "drafted", "reviewed", "final", "published"],
        start=1,
    ):
        st.save_outline(
            ChapterOutline(
                ch=i, title=f"t{i}", pov="mc", scenes=[Scene(at="开场", beat="起")], status=s
            )
        )

    class Args:
        pass

    a = Args()
    a.root = str(ws)
    a.book = str(st.p.root.name)
    # 前三态可确认
    a.chapters = [1, 2, 3]
    assert cmd_confirm(a) == 0
    for ch in (1, 2, 3):
        assert str(st.load_outline(ch).status) == "planned", ch
    # 后四态被拒，且盘上原样
    a.chapters = [4, 5, 6, 7]
    assert cmd_confirm(a) == 1  # 有失败则退出码非 0
    expect = {4: "drafted", 5: "reviewed", 6: "final", 7: "published"}
    for ch, s in expect.items():
        assert str(st.load_outline(ch).status) == s, f"第{ch}章被改写"



# ---------------------------------------------------------------- 缺陷 #77


def test_store_rejects_traversal_in_thread_and_profile_stems(tmp_path: Path):
    """用用户输入拼文件名的三处（线程 id / 平台名 / 角色 id）都要守单段名。

    实测（修复前）：`save_thread(Thread(id="../../evil"))` 把文件写到书根；
    `id="../../book"` 直接覆盖 book.json，整本书从书架消失；
    `id="../../../../../tmp/xxx/evil2"` 甚至能写出书外（文件确实落盘）。
    HTTP 层 `PUT /threads` 收到 `id="../../evil"` 返回 200 且文件真的在书根。
    """
    from server.core.schema import BookMeta, PlatformProfile
    from server.core.store import StoreError, Workbench

    st = Workbench(tmp_path / "ws").create_novel(BookMeta(title="单段名", platform="tomato"))

    # 正常 id 不受影响（模板初始化也走这条）
    st.save_thread(Thread(id="main", name="主线"))
    assert (st.p.threads_dir / "main.json").exists()
    assert [t.id for t in st.load_threads()] == ["main"]

    bad = ["../../evil", "../evil", "..", ".", "a/b", ".." + chr(92) + "evil", ""]
    for tid in bad:
        try:
            st.save_thread(Thread(id=tid, name="坏"))
            raise AssertionError(f"线程 id {tid!r} 应被拒绝")
        except StoreError as exc:
            assert "线程 id" in str(exc), str(exc)

    # 书根不能被塞进杂七杂八的 json，book.json 必须原样
    assert sorted(p.name for p in st.p.root.glob("*.json")) == ["book.json"]
    assert json.loads((st.p.root / "book.json").read_text(encoding="utf-8"))["title"] == "单段名"

    # 平台名同理
    for plat in ["../../evil", "a/b", ".."]:
        try:
            st.save_profile(PlatformProfile(platform=plat))
            raise AssertionError(f"平台名 {plat!r} 应被拒绝")
        except StoreError as exc:
            assert "平台名" in str(exc), str(exc)

    # 角色 id 同理
    for cid in ["../../evil", "a/b", ".."]:
        try:
            st.save_character(CharacterCard(id=cid, name="坏"))
            raise AssertionError(f"角色 id {cid!r} 应被拒绝")
        except StoreError as exc:
            assert "角色 id" in str(exc), str(exc)

    # 正常值仍能写
    st.save_profile(PlatformProfile(platform="qidian"))
    st.save_character(CharacterCard(id="mc", name="主角"))
    assert (st.p.profiles / "qidian.json").exists()
    assert (st.p.characters_dir / "mc.yaml").exists()



# ---------------------------------------------------------------- 缺陷 #78


def test_create_novel_rejects_traversal_in_platform(tmp_path: Path):
    """建书时平台名会拼进 profiles/ 与 workbench/profiles/ 的文件名。

    实测（修复前）：`create_novel(platform="../../evil")` 成功建书，并在
    ws/evil.json（工作区根）与 workbench/profiles/a/b.json 真的落盘；
    `platform="a/b"` 还能在书的 profiles/ 下造出子目录。HTTP 层
    `POST /api/books` 返回 200，越界文件已经写完了。
    """
    from server.core.schema import BookMeta
    from server.core.store import StoreError, Workbench

    wb = Workbench(tmp_path / "ws")

    ok = wb.create_novel(BookMeta(title="正常书", platform="tomato"))
    assert (ok.p.profiles / "tomato.json").exists()

    for plat in ["../../evil", "a/b", "..", ".", ""]:
        try:
            wb.create_novel(BookMeta(title="坏书" + str(abs(hash(plat)) % 997), platform=plat))
            raise AssertionError(f"平台名 {plat!r} 应被拒绝")
        except StoreError as exc:
            assert "平台名" in str(exc), str(exc)

    # 工作区根与 workbench/profiles 下不能有越界产物
    assert sorted(p.name for p in (tmp_path / "ws").glob("*.json")) == []
    shared = (tmp_path / "ws" / "workbench" / "profiles").rglob("*.json")
    assert [p.name for p in shared] == ["tomato.json"], [str(p) for p in shared]


# 缺陷 #99：git 失败不能「只体现在返回值上」——多数调用方拿了就丢
def test_git_failure_is_logged_and_surfaced(tmp_path, caplog):
    """§7.6「失败不静默降级」在 git 这一层的落地。

    实测（修复前）：
    - `commit_all` 的 except 只 `return CommitResult(False, error=...)`，一行日志都没有；
    - `PATCH /foreshadow/{fid}` 更是把返回值整个丢掉——作者改了伏笔状态，
      以为进了版本库，实际 .git 一坏就全没了，界面上什么都不会显示。
    """,
    import json
    import logging

    from fastapi.testclient import TestClient

    from server.app import create_app

    ws = tmp_path / "ws"
    ws.mkdir(parents=True)
    client = TestClient(create_app(ws))
    d = client.post("/api/books", json={"title": "书", "platform": "tomato"}).json()["dir"]
    root = ws / "novels" / d
    (root / "truth").mkdir(parents=True, exist_ok=True)
    (root / "truth" / "foreshadow.json").write_text(
        json.dumps({"items": [{"id": "F-001", "desc": "x", "status": "planted"}]}),
        encoding="utf-8",
    )

    # 对照组：git 正常时 commit.ok 为 True
    ok = client.patch("/api/books/" + d + "/foreshadow/F-001", json={"desc": "y"})
    assert ok.status_code == 200
    assert ok.json()["commit"]["ok"] is True, ok.json()

    # 把 .git 弄坏（外部工具/同步盘干得出来）
    (root / ".git" / "HEAD").write_text("garbage\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="workbench.git"):
        bad = client.patch("/api/books/" + d + "/foreshadow/F-001", json={"status": "hinted"})
    # 真相源照常落盘（git 挂了不能连正文一起丢）
    assert bad.status_code == 200
    assert bad.json()["status"] == "hinted"
    # 但必须留痕 + 在响应里告诉调用方（先查日志，再查响应体：
    # 顺序反了会被 KeyError 挡住，看不出日志那条到底有没有生效）
    # 必须限定 logger：routes 层也会打一条含「git 提交失败」字样的告警，
    # 不限定就会在 gitbot 的日志被摘掉时仍然通过（假绿）。
    msgs = "".join(
        r.getMessage() for r in caplog.records if r.name == "workbench.git"
    )
    assert "git 提交失败" in msgs, msgs
    assert bad.json()["commit"]["ok"] is False


# 缺陷 #126：commit 成功但 tag 没打上时，返回值不能照抄 tag=ch/0002
def test_commit_result_does_not_claim_a_tag_that_was_not_created(tmp_path, caplog):
    """打标失败必须「说真话」：tag 置空 + tag_error 写明原因 + 留 WARNING。

    实测（修复前，探针 .runtime-test/p404.py）：预置一个残留的
    ``.git/refs/tags/ch/0002.lock``（上一次进程被 kill 的典型现场）后：
    - ``commit_chapter(2, ...)`` 返回 ``ok=True, tag='ch/0002', error=''``；
    - 实际 ``tags()`` 里只有 ``ch/0001`` 和 ``ch/0002.lock``；
    - ``show_file_at("ch/0002", ...)`` 返回 None —— 时光机查不到这一章；
    - 一行日志都没有，界面上写着「已提交」。
    """
    import logging

    st = Workbench(tmp_path).create_novel(BookMeta(title="打标书", platform="tomato"))
    root = st.p.root
    bot = GitBot(root)
    assert bot.init().ok

    # 对照组：正常路径 tag 要在，且不带 tag_error
    r1 = bot.commit_chapter(1, "开局", 500)
    assert r1.ok and r1.tag == "ch/0001", r1
    assert not r1.tag_error, r1

    # 打标必失败的现场：残留 lock 文件
    lock = root / ".git" / "refs" / "tags" / "ch" / "0002.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("", encoding="utf-8")
    st.write_chapter(2, "# 第2章\n\n承上启下。")

    with caplog.at_level(logging.WARNING, logger="workbench.git"):
        r2 = bot.commit_chapter(2, "承上", 500)
    # 提交本身是成功的（正文进了版本库，不能因为打标失败就整个回滚）
    assert r2.ok and r2.sha, r2
    # 但不能再声称打了 tag：时光机靠 ch/0002 索引，查不到就等于没打
    assert r2.tag == "", f"打标失败却返回 tag={r2.tag!r}（缺陷 #126）"
    assert r2.tag_error, "打标失败却没给出原因（缺陷 #126）"
    assert "ch/0002" not in bot.tags(), bot.tags()
    msgs = "".join(r.getMessage() for r in caplog.records if r.name == "workbench.git")
    assert "git 打标失败" in msgs, msgs



# 缺陷 #126（第二条打标路径）：commit_paths 也必须说真话
def test_commit_paths_does_not_claim_a_tag_that_was_not_created(tmp_path):
    """``_commit_paths`` 是 curator 沉淀确认走的另一条打标路径。

    反向验证实测：只摘掉这一处的 ``tag="" if tag_err else ...``（保留
    ``_commit_all`` 的），上面那条测试仍然全绿 —— 说明这一条路径无人看守，
    改坏了不会有人报。所以这条测试必须单独存在。
    """
    st = Workbench(tmp_path).create_novel(BookMeta(title="沉淀书", platform="tomato"))
    root = st.p.root
    bot = GitBot(root)
    assert bot.init().ok

    (root / "truth" / "constitution.md").write_text("# 世界观\n\n天道。", encoding="utf-8")
    r1 = bot.commit_paths(["truth/constitution.md"], "truth: 立宪", tag="ch/0001")
    assert r1.ok and r1.tag == "ch/0001" and not r1.tag_error, r1

    lock = root / ".git" / "refs" / "tags" / "ch" / "0002.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("", encoding="utf-8")
    (root / "truth" / "constitution.md").write_text("# 世界观\n\n天道。人道。", encoding="utf-8")

    r2 = bot.commit_paths(["truth/constitution.md"], "truth: 补", tag="ch/0002")
    assert r2.ok and r2.sha, r2
    assert r2.tag == "", f"commit_paths 打标失败却返回 tag={r2.tag!r}（缺陷 #126）"
    assert r2.tag_error, "commit_paths 打标失败却没给出原因（缺陷 #126）"
    assert "ch/0002" not in bot.tags(), bot.tags()


# 缺陷 #106：并发 commit 会静默丢提交（git 的 index.lock 不允许并发）
def test_concurrent_commits_are_not_lost(tmp_path):
    """12 个线程并发 commit_all：实测修复前有 9 个报 index.lock / refs 已存在。

    文件其实都落盘了，但只有少数进了版本库 —— 作者以为自己的真相源进了 git，
    时光机上却查不到。锁必须落在 GitBot 内部，不能指望每个调用方自己加。
    """
    import subprocess
    import threading

    ws = tmp_path / "ws"
    st = Workbench(ws).create_novel(BookMeta(title="并发提交", platform="tomato"))
    root = st.p.root
    bot = GitBot(root)
    assert bot.init().ok

    errors = []

    def worker(i):
        (root / ("f%d.txt" % i)).write_text("c%d" % i, encoding="utf-8")
        r = bot.commit_all("add f%d" % i)
        if not r.ok:
            errors.append((i, r.error))

    ths = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()

    assert not errors, "并发提交失败：%s" % errors[:3]
    # 12 个文件都要能进版本库
    sts = subprocess.run(["git", "status", "--porcelain"], cwd=str(root), capture_output=True, text=True)
    assert sts.stdout.strip() == "", "提交后仍有未跟踪文件：%s" % sts.stdout[:200]


def test_git_lock_is_reentrant_in_same_thread(tmp_path):
    """同一线程内嵌套拿锁不能把自己锁死（缺陷 #106 的配套）。

    flock 对同一进程的第二次 LOCK_EX 也会阻塞，所以 commit_all 内部再调
    commit_paths 这类路径必须可重入。退化为「不加锁」是明确的选择：
    宁可少一层互斥，也不能死锁。
    """
    import time

    t0 = time.time()
    with atomic.file_lock(tmp_path / "a.txt"):
        with atomic.file_lock(tmp_path / "a.txt"):
            pass
    assert time.time() - t0 < 2.0, "同线程嵌套拿锁把自己锁死了"


# ---------------------------------------------------------------- 缺陷 #108 CLI 发布断层


def test_cli_publish_prints_gap_warning(tmp_path, capsys):
    """CLI 的 `moran publish` 必须把发布断层说出口。

    #70 让服务端 mark_published 回传 gaps / warning，#105 让 Web 端（DailyView）
    消费它，但 CLI 只打印 updated —— 走终端连载的作者看到「标记发布: [5]」就以为
    一切正常，而读者端第 1~4 章是空的。同一条告警不能在某个入口被丢弃。
    """
    from server.core.store import Workbench
    from server.core.schema import BookMeta, ChapterOutline, Scene
    from server.cli import cmd_publish

    ws = tmp_path / "ws"
    st = Workbench(ws).create_novel(BookMeta(title="CLI发布", platform="tomato"))
    for i in range(1, 6):
        st.save_outline(
            ChapterOutline(
                ch=i, title=f"t{i}", pov="mc", scenes=[Scene(at="开场", beat="起")], status="final"
            )
        )

    class Args:
        pass

    a = Args()
    a.root = str(ws)
    a.book = str(st.p.root.name)
    a.chapters = [5]
    a.unpublish = False
    assert cmd_publish(a) == 0
    out = capsys.readouterr().out
    assert "标记发布" in out
    # 第 1~4 章还没发布，必须告警；只报 updated 等于把断层藏起来
    assert "断层" in out, "CLI 丢弃了 mark_published 的断层告警：%r" % out
    assert "1" in out and "4" in out, "告警里要给出具体缺哪几章：%r" % out

    # 顺序发布（先 1~4 再 5）不该再有告警
    capsys.readouterr()
    a2 = Args()
    a2.root = str(ws)
    a2.book = str(st.p.root.name)
    a2.chapters = [1, 2, 3, 4]
    a2.unpublish = False
    assert cmd_publish(a2) == 0
    out2 = capsys.readouterr().out
    assert "断层" not in out2, "补齐后仍在告警：%r" % out2


# ---------------------------------------------------------------- 缺陷 #113 CLI 流水线静默成功


def _mock_gateway(ctx):
    """把网关换成纯 mock（离线确定性）。"""
    from server.model.base import RouteHop, RoutingTable
    from server.model.providers.mock import MockProvider
    from server.model.providers.registry import ProviderRegistry, ProviderSpec

    reg = ProviderRegistry()
    reg.providers["mock-local"] = MockProvider(name="mock-local")
    reg.specs["mock-local"] = ProviderSpec(name="mock-local", kind="mock", models=["mock-1"])
    keys = ("plan", "write", "review", "curator", "polish", "summarize", "deconstruct", "free")
    ctx.gateway.registry = reg
    ctx.gateway.routing = RoutingTable(
        routes={k: [RouteHop("mock-local", "mock-1")] for k in keys}
    )
    ctx.invalidate()


def test_cli_pipeline_returns_nonzero_when_a_chapter_fails(tmp_path, capsys):
    """缺陷 #113：`moran pipeline` 的每一步失败都被 try/except 吞成一行 print。

    实测：把第 1 章置为 published 再跑 pipeline，输出里密密麻麻六行「失败」，
    结尾是「=== 完成 ===」、退出码 0。crontab / CI 只看退出码 —— 半夜批量跑
    20 章，19 章炸了，收件箱里只有一句「完成」。告警要走到进程退出码这一层。
    """
    from server.api.deps import AppContext
    from server.cli import cmd_pipeline
    from server.core.schema import BookMeta, ChapterOutline, Scene

    ws = tmp_path / "ws"
    ctx = AppContext.create(ws)
    st = ctx.workbench.create_novel(BookMeta(title="流水线", platform="tomato"))
    _mock_gateway(ctx)
    bc = ctx.book(st.p.root.name)
    for ch in (1, 2):
        bc.store.save_outline(
            ChapterOutline(
                ch=ch,
                title=f"t{ch}",
                pov="mc",
                scenes=[Scene(at="开场", beat="起")],
                hook="钩",
                status="draft",
            )
        )
    # 第 1 章已发布 → Writer 必然拒绝（#75/#76 的正当保护）
    o1 = bc.store.load_outline(1)
    o1.status = "published"
    bc.store.save_outline(o1)

    class Args:
        pass

    a = Args()
    a.root = str(ws)
    a.book = st.p.root.name
    a.chapters = (1, 2)
    a.no_model = False

    rc = cmd_pipeline(a)
    out = capsys.readouterr().out
    assert "失败" in out, f"这一章必然失败，输出里却没有：{out!r}"
    assert rc != 0, f"流水线有步骤失败却返回 {rc} —— 退出码没有承载失败（缺陷 #113）"




def test_curate_confirm_returns_nonzero_when_a_proposal_fails(tmp_path, capsys):
    """缺陷 #114：提案一条都没落下去，`curate-confirm` 照样返回 0。

    Curator.confirm 只把落盘失败塞进 warnings，返回值里没有失败计数; CLI 打完
    「已应用 0 项，跳过 1 项」就 return 0。作者(和套着它的脚本)以为沉淀完成了,
    实际上 truth 一行没改 —— 和 #113 是同一类「告警没走到进程出口」。
    """
    from server.api.deps import AppContext
    from server.cli import cmd_curate_confirm
    from server.core import atomic
    from server.core.schema import BookMeta

    ws = tmp_path / "ws"
    ctx = AppContext.create(ws)
    st = ctx.workbench.create_novel(BookMeta(title="沉淀", platform="tomato"))
    bc = ctx.book(st.p.root.name)
    pack = bc.store.p.root / "memory" / "proposals-0001.json"
    # book.json 在白名单外(缺陷 #103/#104 的下沉门槛), 必然被拒绝
    bad = [
        {
            "id": "p1",
            "file": "book.json",
            "op": "replace",
            "reason": "改书名",
            "content": "x",
        }
    ]
    atomic.write_json_atomic(pack, {"ch": 1, "proposals": bad})

    class Args:
        pass

    a = Args()
    a.root = str(ws)
    a.book = st.p.root.name
    a.ch = 1
    a.ids = None
    a.no_commit = True

    rc = cmd_curate_confirm(a)
    out = capsys.readouterr().out
    assert "落盘失败" in out, "这一条必然被白名单拒绝: " + repr(out)
    assert rc != 0, "有提案落盘失败却返回 %d —— 沉淀结果没有进退出码(缺陷 #114)" % rc
    # 服务端返回值本身也要带失败计数, 否则 API 调用方照样看不出来
    atomic.write_json_atomic(pack, {"ch": 1, "proposals": bad})
    again = bc.curator.confirm(1, None, commit=False)
    assert again.get("failed") == 1, "confirm 返回值里没有失败计数: " + str(sorted(again))


def test_avoidance_section_is_capped_and_reports_trimming(tmp_path):
    """缺陷 #115：truth 的「本章避雷」只增不减。

    每条避雷条目都带章号(实测: `- [第1章] 钩子弱 -> 后续章节避免: ...`),
    去重按整行精确匹配 —— 只要章号/日期不同就是新条目, 于是这一节随写作
    无限增长。Planner._avoidance_brief 只读最近 12 条, 剩下的既不生效,
    又永远躺在真相源文件里(作者打开 style-fingerprint.md 看到一屏陈年旧账)。
    上界与 #58/#68/#82 同源: 留存最近的 N 条, 并如实告诉作者丢了几条。
    """
    from server.api.deps import AppContext
    from server.core.schema import BookMeta
    from server.feedback.ingest import MAX_AVOIDANCE_ENTRIES

    ws = tmp_path / "ws"
    ctx = AppContext.create(ws)
    st = ctx.workbench.create_novel(BookMeta(title="避雷", platform="tomato"))
    bc = ctx.book(st.p.root.name)
    path = bc.store.p.style_fingerprint

    n = MAX_AVOIDANCE_ENTRIES + 20
    last = None
    for i in range(n):
        last = bc.feedback.apply_proposals(
            [{"id": "e%d" % i, "entry": "- [第{0}章] 避雷条目 {0}".format(i)}]
        )

    assert last["applied"] == 1, "最后一条必然是新条目: " + str(last)
    assert last["trimmed"] > 0, "超出上界却没有报告丢弃条数: " + str(last)
    assert last["limit"] == MAX_AVOIDANCE_ENTRIES

    import re

    txt = path.read_text(encoding="utf-8")
    m = re.search(r"^## 本章避雷\s*$", txt, re.MULTILINE)
    assert m, "写完之后连「本章避雷」这一节都没有了"
    body = txt[m.end():]
    kept = [l for l in body.splitlines() if l.strip()]
    assert len(kept) <= MAX_AVOIDANCE_ENTRIES, "留存 %d 条超过上界 %d" % (len(kept), MAX_AVOIDANCE_ENTRIES)
    # 丢的是最旧的, 最新的必须还在
    assert "避雷条目 %d" % (n - 1) in txt
    assert "避雷条目 0\n" not in txt, "最旧的条目没被丢掉"


def test_names_pool_header_written_once_under_concurrency(tmp_path: Path):
    """缺陷 #116：并发首次起名会各写一份表头。

    _append_pool 原实现是「exists() 判表头 -> open("a") 裸追加」，两步之间无锁，
    N 个线程同时看到文件不存在就各写一份表头，Markdown 中段表格渲染断裂。
    与 #88 同源：当年只把灵感速记换成了 append_text，起名词池漏了。
    """
    import threading

    wb = Workbench(tmp_path)
    store = wb.create_novel(BookMeta(title="词池"))
    gen = NameGenerator(store)
    pool_file = store.p.materials / "dictionary" / "names-pool.md"

    n = 24
    errs = []

    # 单轮只有约 15% 概率复现（无锁代码的两个线程恰好都读到「文件不存在」），
    # 所以连跑 15 轮：无锁实现几乎必现（0.85**15 ~= 8.7%），持锁实现恒为 1 份。
    for _round in range(15):
        if pool_file.exists():
            pool_file.unlink()
        bar = threading.Barrier(n)

        def work(i):
            bar.wait()
            try:
                gen.generate(kind="sect", count=2, seed=i, save=True)
            except Exception as exc:
                errs.append(repr(exc))

        ts = [threading.Thread(target=work, args=(i,)) for i in range(n)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()

        text = pool_file.read_text(encoding="utf-8")
        heads = [ln for ln in text.splitlines() if ln.startswith("| 名字 |")]
        assert len(heads) == 1, f"第 {_round} 轮表头被写了 {len(heads)} 份，中段表格会断裂"
        assert text.startswith("# 起名词池"), "表头内容被改写"

    assert not errs, f"并发起名抛错: {errs[:3]}"
    # 所有候选都还在（不能为了消重复把数据也丢了）
    assert len(gen.read_pool()) >= n, "并发写入丢了候选名"


def test_concurrent_proposal_appends_are_not_lost(tmp_path):
    """缺陷 #117：apply_proposal 是「读旧内容 -> 拼 -> 写回」，不持锁就互相覆盖。

    实测（修复前）：12 条并发 append 到 entity-deltas.jsonl 只活 6 条，
    truth/timeline.yaml 只活 4 条，且**全程无异常**——Curator 的 confirm()
    把每条都记成 applied，作者以为 12 条伏笔/时间线都沉淀了，实际一半蒸发。
    修复：整段包在 atomic.file_lock(target) 里，锁按目标文件粒度。
    """
    import threading



    ws = tmp_path / "ws117"
    wb = Workbench(ws)
    store = wb.create_novel(BookMeta(title="并发沉淀", platform="tomato"))

    def mk_delta(i):
        return Proposal(
            id="p%d" % i,
            file="memory/entity-deltas.jsonl",
            op="append",
            lines=[json.dumps({"ch": i, "entity": "mc", "kind": "state", "op": "update", "note": "n%d" % i},
                             ensure_ascii=False)],
            reason="probe",
        )

    def mk_timeline(i):
        return Proposal(
            id="t%d" % i,
            file="truth/timeline.yaml",
            op="append",
            patch={"ch": i, "date": "2026-01-%02d" % (i % 28 + 1), "event": "e%d" % i},
            reason="probe",
        )

    n = 12
    # 基线：顺序追加必须全在
    for i in range(3):
        store.apply_proposal(mk_delta(i))
        store.apply_proposal(mk_timeline(i))
    base_d = len(store.read_entity_deltas())
    base_t = len(store.load_timeline())
    assert base_d == 3 and base_t == 3, "顺序追加就不该丢：%d/%d" % (base_d, base_t)

    bar = threading.Barrier(n * 2)
    errs = []

    def work(i, factory):
        try:
            bar.wait()
            store.apply_proposal(factory(i))
        except Exception as exc:
            errs.append(repr(exc))

    ts = []
    for i in range(n):
        ts.append(threading.Thread(target=work, args=(100 + i, mk_delta)))
        ts.append(threading.Thread(target=work, args=(200 + i, mk_timeline)))
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    assert not errs, "并发落盘抛错: %s" % errs[:3]
    got_d = len(store.read_entity_deltas())
    got_t = len(store.load_timeline())
    assert got_d == 3 + n, "实体增量丢了：期望 %d 条，实际 %d 条" % (3 + n, got_d)
    assert got_t == 3 + n, "时间线条目丢了：期望 %d 条，实际 %d 条" % (3 + n, got_t)



def test_cli_index_verify_returns_nonzero_on_drift(tmp_path, monkeypatch, capsys):
    """缺陷 #119：``moran index verify`` 是给 cron / CI 用的。

    对账不通过（漂移 / 缺章 / 孤儿 / 实体数据源损坏）时必须体现在退出码上，
    否则半夜跑出来一段 JSON，没人看，等于没对账。
    """
    import argparse

    from server.api.deps import AppContext
    from server.core.schema import BookMeta

    ws = tmp_path / "ws119"
    ctx = AppContext.create(ws)
    ctx.workbench.create_novel(BookMeta(title="对账", platform="tomato"))
    d = ctx.workbench.refresh_shelf().novels[0].dir
    store = ctx.book(d).store
    store.write_chapter(1, "第一章正文，够长就行。" * 20)
    ctx.book(d).retrieval.build_index(full=True)

    import server.cli as C
    monkeypatch.setattr(C, "_ctx", lambda a: ctx)

    args = argparse.Namespace(root=str(ws), book=d, action="verify")
    rc_clean = C.cmd_index(args)
    assert rc_clean == 0, "干净的对账应当返回 0"

    # 改正文但不重索引 —— 制造漂移
    store.write_chapter(1, "第一章正文改过了。" * 25)
    args2 = argparse.Namespace(root=str(ws), book=d, action="verify")
    rc_drift = C.cmd_index(args2)
    out = capsys.readouterr().out
    assert rc_drift != 0, "对账不通过却返回 0"
    assert "正文已改未重索引" in out, out


def test_cli_index_verify_reports_missing_vectors(tmp_path, monkeypatch, capsys):
    """缺陷 #127：向量缺失时 hash 对账照样全绿，cron 必须能看见。

    制造方式：配一个真 embedder 让索引「应该有向量」，再在 build 前把向量表清空，
    于是 chunks>0 且 vectors==0 且 embedder 不是 None → 对账不通过 + 退出码非 0。
    """
    import argparse

    from server.api.deps import AppContext
    from server.core.schema import BookMeta
    from server.retrieval.embedder import HashEmbedder

    ws = tmp_path / "ws127"
    ctx = AppContext.create(ws)
    ctx.workbench.create_novel(BookMeta(title="向量对账", platform="tomato"))
    d = ctx.workbench.refresh_shelf().novels[0].dir
    store = ctx.book(d).store
    store.write_chapter(1, "第一章正文，够长就行。" * 20)
    bc = ctx.book(d)
    bc.retrieval.build_index(full=True)

    # 基线：没配 embedder 时零向量是预期，不该红
    import server.cli as C
    monkeypatch.setattr(C, "_ctx", lambda a: ctx)
    rc_base = C.cmd_index(argparse.Namespace(root=str(ws), book=d, action="verify"))
    assert rc_base == 0, "没配 embedding 不该让对账天天红"

    # 换成「配了 embedder 却没向量」：模拟 embedding 跑挂后的现场
    from server.retrieval.facade import RetrievalFacade
    from server.retrieval.index import IndexBuilder

    idx = IndexBuilder(store, embedder=HashEmbedder())
    idx.build(full=True)
    assert idx.stats().vectors > 0, "对照组：向量本来是建出来了的"
    conn = __import__("sqlite3").connect(str(idx.db_path))
    conn.execute("DELETE FROM vectors")
    conn.commit()
    conn.close()
    assert idx.stats().vectors == 0
    bc.retrieval = RetrievalFacade(store, embedder=HashEmbedder())

    rc = C.cmd_index(argparse.Namespace(root=str(ws), book=d, action="verify"))
    out = capsys.readouterr().out
    assert rc != 0, "向量缺失却返回 0，cron 看不见"
    assert "向量缺失" in out, out
