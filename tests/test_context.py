"""上下文装配引擎单测（§5 六层 + 预算裁剪 + 硬错误 + 落盘审计）。"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from server.context.assembler import (
    AssemblyError,
    ContextAssembler,
    estimate_tokens,
    save_assembly,
    truncate_to_tokens,
)
from server.core.schema import (
    BookMeta,
    ChapterOutline,
    CharacterCard,
    ForeshadowBook,
    ForeshadowItem,
    PowerScale,
    Scene,
)
from server.core.store import NovelStore, Workbench


# ---------------------------------------------------------------- 假检索

class FakeHit:
    def __init__(self, ch: int, text: str):
        self.ch = ch
        self.text = text
        self.citation = f"[ch-{ch:04d}:段1]"


class FakeRetrievalResult:
    def __init__(self, hits: list[FakeHit], mode: str = "hybrid"):
        self.hits = hits
        self.mode = mode
        self.degraded = mode != "hybrid"
        self.rendered = "\n\n".join(f"{h.citation} {h.text}" for h in hits)


class FakeRetrieval:
    def __init__(self, hits=None, available=True, raises=False):
        self.available = available
        self._hits = hits or []
        self._raises = raises

    async def search_for_outline(self, outline, top_k=8, budget_chars=6000):
        if self._raises:
            raise RuntimeError("boom")
        return FakeRetrievalResult(self._hits)


def _book(tmp_path: Path, **kw) -> NovelStore:
    wb = Workbench(tmp_path)
    return wb.create_novel(BookMeta(title="装配测试", **kw))


def _outline(ch: int = 4) -> ChapterOutline:
    return ChapterOutline(
        ch=ch,
        title="淬骨",
        pov="mc",
        threads=["main"],
        scenes=[
            Scene(loc="world/geography#青岩矿脉", chars=["mc", "elder-wang"], goal="夺矿", conflict="使绊", outcome="险胜")
        ],
        must_include=["@characters/elder-wang 首次登场"],
        hook="矿洞深处传来诵经声",
        target_words=(2000, 2400),
        status="planned",
    )


# ---------------------------------------------------------------- token 工具


def test_estimate_and_truncate():
    assert estimate_tokens("") == 0
    assert estimate_tokens("你好") == 2
    long = "第一句话。第二句话。第三句话。" * 100
    out = truncate_to_tokens(long, 50)
    assert estimate_tokens(out) <= 60
    assert out.endswith(("。", "！", "？", "\n")) or len(out) < len(long)


# ---------------------------------------------------------------- 硬错误


def test_l0_missing_is_hard_error(tmp_path: Path):
    store = _book(tmp_path)
    store.p.constitution.unlink()
    store.p.style_fingerprint.unlink()
    asm = ContextAssembler(store)
    with pytest.raises(AssemblyError) as ei:
        asyncio.run(asm.assemble(_outline()))
    assert "L0" in str(ei.value)


def test_l1_empty_is_hard_error(tmp_path: Path):
    store = _book(tmp_path)
    asm = ContextAssembler(store)
    empty = ChapterOutline(ch=5)  # 无 scenes / must_include / hook
    with pytest.raises(AssemblyError) as ei:
        asyncio.run(asm.assemble(empty))
    assert "L1" in str(ei.value)


# ---------------------------------------------------------------- 六层内容


def test_all_layers_built(tmp_path: Path):
    store = _book(tmp_path)
    store.save_character(
        CharacterCard(
            id="mc",
            name="林澈",
            personality=["外冷内热"],
            boundaries=["绝不主动屠杀平民"],
            power_scale=PowerScale(rank="淬体七重", forbidden="凝气期"),
        )
    )
    store.save_character(CharacterCard(id="elder-wang", name="王长老", role="antagonist"))
    store.save_foreshadow(
        ForeshadowBook(
            items=[
                ForeshadowItem(id="F-001", desc="残图与碑文同源", planted_ch=3, status="planted", deadline_ch=40)
            ]
        )
    )
    store.write_summary(3, "第3章：主角入门，得到残图。")
    store.write_chapter(3, "正文" * 400 + "结尾：他抬头看向山门。")

    outline = _outline(4)
    outline.foreshadow_actions = [{"ref": "F-001", "action": "hint", "note": "矿壁纹路"}]
    store.save_outline(outline)

    hits = [FakeHit(1, "旧文片段一，林澈第一次握剑。"), FakeHit(2, "旧文片段二，王长老登场。")]
    asm = ContextAssembler(store, retrieval=FakeRetrieval(hits))
    ctx = asyncio.run(asm.assemble(outline))

    assert ctx.layers["L0"].content and "红线" in ctx.layers["L0"].content
    assert "第 4 章" in ctx.layers["L1"].content
    assert "林澈" in ctx.layers["L1"].content  # 出场人物卡
    assert "行为边界" in ctx.layers["L1"].content
    assert "F-001" in ctx.layers["L2"].content
    assert "hint" in ctx.layers["L2"].content
    assert "第 3 章" in ctx.layers["L3"].content
    assert "[ch-0001:段1]" in ctx.layers["L4"].content
    assert "elder-wang" in ctx.layers["L5"].content  # @引用解析
    assert ctx.total_tokens <= ctx.budget
    # 提示词骨架（§5.2）
    assert "世界观红线（L0）" in ctx.system_prompt
    assert "第 4 章" in ctx.user_prompt


def test_l2_empty_when_no_active_foreshadow(tmp_path: Path):
    store = _book(tmp_path)
    store.save_character(CharacterCard(id="mc", name="林澈"))
    outline = _outline(4)
    asm = ContextAssembler(store)
    ctx = asyncio.run(asm.assemble(outline))
    assert ctx.layers["L2"].empty()


def test_no_retrieval_degrades_not_raises(tmp_path: Path):
    store = _book(tmp_path)
    store.save_character(CharacterCard(id="mc", name="林澈"))
    asm = ContextAssembler(store, retrieval=None)
    ctx = asyncio.run(asm.assemble(_outline()))
    assert ctx.layers["L4"].empty()
    assert any("L4" in w for w in ctx.warnings)


def test_retrieval_index_not_ready_degrades(tmp_path: Path):
    store = _book(tmp_path)
    store.save_character(CharacterCard(id="mc", name="林澈"))
    asm = ContextAssembler(store, retrieval=FakeRetrieval(available=False))
    ctx = asyncio.run(asm.assemble(_outline()))
    assert ctx.layers["L4"].empty()


def test_retrieval_exception_degrades(tmp_path: Path):
    store = _book(tmp_path)
    store.save_character(CharacterCard(id="mc", name="林澈"))
    asm = ContextAssembler(store, retrieval=FakeRetrieval(raises=True))
    ctx = asyncio.run(asm.assemble(_outline()))  # 不抛
    assert ctx.layers["L4"].empty()
    assert any("检索失败" in w for w in ctx.warnings)


# ---------------------------------------------------------------- 预算


def test_budget_trim_keeps_l0_l1(tmp_path: Path):
    store = _book(tmp_path)
    store.save_character(CharacterCard(id="mc", name="林澈", boundaries=["x" * 50]))
    store.write_summary(3, "摘要" * 2000)
    store.write_chapter(3, "正文" * 3000)
    hits = [FakeHit(i, "旧文" * 800) for i in range(1, 6)]
    asm = ContextAssembler(store, retrieval=FakeRetrieval(hits), budget=1200)
    ctx = asyncio.run(asm.assemble(_outline()))
    # L0/L1 不可裁
    assert not ctx.layers["L0"].empty() and not ctx.layers["L0"].trimmed
    assert not ctx.layers["L1"].empty()
    # L4 先被裁
    assert ctx.layers["L4"].trimmed or ctx.layers["L4"].empty()
    assert any("预算" in w or "裁剪" in w for w in ctx.warnings)


def test_assembly_never_exceeds_budget_when_possible(tmp_path: Path):
    store = _book(tmp_path)
    store.save_character(CharacterCard(id="mc", name="林澈"))
    for ch in range(1, 4):
        store.write_summary(ch, "第%d章摘要。" % ch * 300)
    hits = [FakeHit(i, "旧文" * 400) for i in range(1, 4)]
    asm = ContextAssembler(store, retrieval=FakeRetrieval(hits), budget=3000)
    ctx = asyncio.run(asm.assemble(_outline(4)))
    assert ctx.total_tokens <= 3000 + 50  # 允许估算误差


# ---------------------------------------------------------------- 审计落盘


def test_audit_written_and_traceable(tmp_path: Path):
    store = _book(tmp_path)
    store.save_character(CharacterCard(id="mc", name="林澈"))
    asm = ContextAssembler(store)
    ctx = asyncio.run(asm.assemble(_outline()))
    rel = save_assembly(store, ctx)
    assert rel == "memory/ch-0004.assembled.md"
    text = store.p.assembled_md(4).read_text(encoding="utf-8")
    assert "分层用量" in text
    assert "总 tokens" in text
    assert "L0" in text and "L1" in text
    # 每层内容都落盘可溯源
    assert "世界观红线（L0）" in text


def test_foreshadow_overdue_alert_in_audit(tmp_path: Path):
    store = _book(tmp_path)
    store.save_character(CharacterCard(id="mc", name="林澈"))
    store.save_foreshadow(
        ForeshadowBook(items=[ForeshadowItem(id="F-009", desc="过期伏笔", planted_ch=1, status="planted", deadline_ch=2)])
    )
    asm = ContextAssembler(store)
    ctx = asyncio.run(asm.assemble(_outline(10)))
    assert any(a["id"] == "F-009" for a in ctx.foreshadow_alerts)
    assert "F-009" in ctx.audit_md


def test_l5_resolves_at_refs_across_truth(tmp_path: Path):
    store = _book(tmp_path)
    store.save_character(CharacterCard(id="mc", name="林澈"))
    outline = _outline(4)
    outline.must_include = ["@world/power-system.md 必须遵循境界阶梯"]
    asm = ContextAssembler(store)
    ctx = asyncio.run(asm.assemble(outline))
    assert "境界阶梯" in ctx.layers["L5"].content


def test_l3_degrades_when_no_summaries(tmp_path: Path):
    store = _book(tmp_path)
    store.save_character(CharacterCard(id="mc", name="林澈"))
    store.write_chapter(3, "第3章正文" * 100)
    asm = ContextAssembler(store)
    ctx = asyncio.run(asm.assemble(_outline(4)))
    # 无摘要时取正文开头 + 结尾原文
    assert "第3章正文" in ctx.layers["L3"].content


# ---------------------------------------------------- §5.3 few-shot 片段注入


def _snippet(store: NovelStore, kind: str, name: str, body: str) -> None:
    d = store.p.materials / "snippets" / kind
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(body, encoding="utf-8")


def test_fewshot_injects_snippets_by_scene_kind(tmp_path: Path):
    """§5.3：按场景类型从 materials/snippets/<kind>/ 取 1–2 条做范例。"""
    store = _book(tmp_path)
    _snippet(store, "combat", "a.md", "他横剑于胸，脚下碎石炸开，剑锋擦着颈侧过去。")
    _snippet(store, "combat", "b.md", "两人几乎同时动，拳风撞在一起，墙灰簌簌落下。")
    o = _outline(41)
    o.title = "断崖血战"
    o.scenes = [Scene(loc="断崖", chars=["mc"], goal="击退追兵", conflict="对方持刀围攻", outcome="险胜")]
    store.save_outline(o)

    ctx = asyncio.run(ContextAssembler(store).assemble(o))
    layer = ctx.layers["L4f"]
    assert not layer.empty(), "有 combat 片段时应注入"
    assert "剑" in layer.content or "拳" in layer.content
    assert "范例片段" in ctx.user_prompt
    assert ctx.meta["fewshot"]["picked"] >= 1


def test_fewshot_style_ref_forces_specific_file(tmp_path: Path):
    """章纲 style_ref 可强制指定片段文件（作者手挑的样本优先）。"""
    store = _book(tmp_path)
    _snippet(store, "scene", "rain.md", "雨把整条街洗成灰的，只有灯笼是红的。")
    o = _outline(42)
    o.style_ref = "scene/rain.md"
    store.save_outline(o)

    ctx = asyncio.run(ContextAssembler(store).assemble(o))
    assert "雨把整条街洗成灰的" in ctx.layers["L4f"].content
    assert any("rain.md" in s for s in ctx.layers["L4f"].source)


def test_fewshot_bad_style_ref_warns_but_does_not_fail(tmp_path: Path):
    """style_ref 指向不存在的文件：记 warning，装配继续（不能因此拒生成）。"""
    store = _book(tmp_path)
    o = _outline(43)
    o.style_ref = "combat/does-not-exist.md"
    store.save_outline(o)

    ctx = asyncio.run(ContextAssembler(store).assemble(o))
    assert any("style_ref" in w for w in ctx.warnings)
    assert ctx.system_prompt  # 装配仍然成功


def test_fewshot_empty_dir_is_not_an_error(tmp_path: Path):
    """没有 snippets 是默认状态，不能报错也不能占预算。"""
    store = _book(tmp_path)
    ctx = asyncio.run(ContextAssembler(store).assemble(_outline(44)))
    assert ctx.layers["L4f"].empty()
    assert ctx.layers["L4f"].tokens == 0
# ------------------------------------------------- 防抄袭护栏（QUICKSTART §7）


def test_prompt_forbids_reusing_fewshot_content(tmp_path: Path):
    """QUICKSTART:152 承诺「范例只用于学句式与节奏，prompt 里明确禁止复用其人物、地名、情节」。

    实现在 assembler.py 的系统提示里（写作要求一节）。此前 tests/ 下零命中——
    这条护栏是防抄袭的，被人顺手删掉一行也不会有任何测试变红。
    """
    store = _book(tmp_path)
    asm = ContextAssembler(store)
    ctx = asyncio.run(asm.assemble(_outline()))
    system = ctx.system_prompt or ""

    # 三要素必须逐个点名，不能只写一句「不要抄袭」糊弄过去
    assert "不得复用" in system, "缺少防复用总述"
    assert "人物" in system and "地名" in system and "情节" in system, (
        f"人物/地名/情节必须逐个点名，实际片段：{system[-200:]!r}"
    )
    assert "范例片段" in system, "这条约束要挂在「范例片段」上，而不是泛泛而谈"

    # 反向验证锚点：段落骨架在，改坏其中任一词都会红
    assert "句式、节奏、对话密度与叙事距离" in system



def test_context_budget_is_hard_and_trim_reports(tmp_path):
    """PROJECT.md §1：6 层上下文「总量硬上限 16k」——要有测试盯着它。"""
    import asyncio

    from server.api.deps import AppContext
    from server.context.assembler import ContextAssembler
    from server.core.schema import BookMeta, ChapterOutline

    ctx = AppContext.create(tmp_path / "ws")
    bn = ctx.workbench.create_novel(BookMeta(title="预算", platform="tomato"))
    store = bn.store if hasattr(bn, "store") else bn
    # 灌满真相源，让 6 层内容远超默认预算
    (store.p.truth / "style-fingerprint.md").write_text("风" * 20000, encoding="utf-8")
    (store.p.truth / "constitution.md").write_text("宪" * 20000, encoding="utf-8")
    (store.p.truth / "world-state.md").write_text("世" * 20000, encoding="utf-8")
    (store.p.truth / "characters").mkdir(parents=True, exist_ok=True)
    (store.p.truth / "characters" / "mc.json").write_text(
        '{"id":"mc","name":"主角","role":"protagonist","traits":["坚"*5000]}', encoding="utf-8"
    )
    outline = ChapterOutline(
        ch=1,
        title="起",
        status="drafted",
        scenes=[{"at": "山下", "what": "入门", "pov": "mc"}],
        hook="山中 Old 人出现",
    )
    # 预算 16000 时：要么在预算内，要么明确报告超限（不能静默塞进去）
    asm = ContextAssembler(store, budget=16000)
    r = asyncio.run(asm.assemble(outline))
    assert r.budget == 16000
    if r.total_tokens > r.budget:
        assert r.warnings, "超限必须有告警（不能静默塞进去）"
        assert any("预算" in w for w in r.warnings), r.warnings
    # 极小预算：L0/L1 不可裁，必须报「裁剪后仍超预算」
    asm2 = ContextAssembler(store, budget=1)
    r2 = asyncio.run(asm2.assemble(outline))
    assert r2.total_tokens > r2.budget
    assert any("裁剪后仍超预算" in w for w in r2.warnings), r2.warnings
    # 「超限/合规」这一句写在审计溯源里（assembler.py:587），不是 system_prompt
    assert "超限" in r2.audit_md, r2.audit_md[-200:]


# ---------------------------------------------------------------- 缺陷 #81

def test_ref_resolution_refuses_dotfiles_and_absolute_paths(tmp_path):
    """@引用 直接拼进文件系统路径：`.git/config`、`.env` 这类隐藏文件本不该进上下文。""",
    ws = tmp_path / "ws"
    st = Workbench(ws).create_novel(BookMeta(title="引用", platform="tomato"))
    asm = ContextAssembler(st)
    # 书内放一份不该被读到的敏感内容
    (st.p.root / ".git").mkdir(parents=True, exist_ok=True)
    (st.p.root / ".git" / "config").write_text("url = https://u:SECRET_TOKEN@github.com/x/y.git\n", encoding="utf-8")
    (st.p.root / ".env").write_text("API_KEY=LEAKED_KEY_VALUE\n", encoding="utf-8")
    # 正常引用必须照旧解析
    (st.p.truth / "mc.md").write_text("人物卡正文", encoding="utf-8")
    for good, rel in [("truth/mc.md", "truth/mc.md"), ("mc", "truth/mc.md")]:
        p = asm._resolve_ref(good)
        assert p is not None, good
        assert p.relative_to(st.p.root).as_posix() == rel, good
    # 隐藏文件 / 绝对路径 / 盘符 / 穿越 一律拒绝
    for bad in [".git/config", ".env", ".env.md", "truth/.hidden.md", "/etc/passwd",
                "C:" + chr(92) + "evil.md", "characters/../../evil", "a/../.env"]:
        assert asm._resolve_ref(bad) is None, bad
    # style_ref 走同一套守卫
    snip = st.p.materials / "snippets"
    snip.mkdir(parents=True, exist_ok=True)
    (snip / "combat.md").write_text("打斗片段", encoding="utf-8")
    assert asm._resolve_snippet_ref("combat", snip) is not None
    assert asm._resolve_snippet_ref(".env", snip) is None


def test_l5_never_leaks_dotfile_into_context(tmp_path):
    """端到端兜底：即使 must_include 里写了 @.env，装配出来的正文也不能带出内容。""",
    ws = tmp_path / "ws"
    st = Workbench(ws).create_novel(BookMeta(title="引用", platform="tomato"))
    asm = ContextAssembler(st)
    (st.p.root / ".env").write_text("API_KEY=LEAKED_KEY_VALUE\n", encoding="utf-8")
    (st.p.root / ".git").mkdir(parents=True, exist_ok=True)
    (st.p.root / ".git" / "config").write_text("url = https://u:SECRET_TOKEN@github.com/x/y.git\n", encoding="utf-8")
    o = ChapterOutline(
        ch=1,
        title="起",
        pov="mc",
        scenes=[Scene(at="开场", beat="起")],
        must_include=["看看 @.git/config 与 @.env"],
        status="drafted",
    )
    lay = asm._build_l5(o)
    assert "SECRET_TOKEN" not in lay.content
    assert "LEAKED_KEY_VALUE" not in lay.content
    assert "未找到" in lay.content

def test_load_book_failure_warns_about_context_budget(tmp_path):
    """#130: load_book 挂了，静默用默认 16000，作者改的 context_budget 被悄悄忽略。
    修复后：warnings 必须含「book.yaml 读取失败…上下文预算已降级为默认 16000」。"""
    store = _book(tmp_path)
    store.p.book_json.write_text('not: valid: yaml: [', encoding='utf-8')
    assembler = ContextAssembler(store)
    ctx = asyncio.run(assembler.assemble(_outline()))
    assert any('book.yaml 读取失败' in w and '上下文预算' in w for w in ctx.warnings), (
        f"#130: load_book 挂了却没警告，warnings={ctx.warnings}"
    )


def test_load_book_ok_no_budget_warning(tmp_path):
    """对照组：book.yaml 正常时不该有预算警告。"""
    store = _book(tmp_path)
    assembler = ContextAssembler(store)
    ctx = asyncio.run(assembler.assemble(_outline()))
    budget_warnings = [w for w in ctx.warnings if 'book.yaml 读取失败' in w]
    assert not budget_warnings, f"#130: 正常时不该有预算警告，实际={budget_warnings}"
