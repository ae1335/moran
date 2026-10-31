"""起名生成器单测（§10.3：Planner 附属工具，产物进 materials/dictionary/）。"""
from __future__ import annotations

from pathlib import Path

import pytest

from server.agents.naming import KIND_LABELS, NameGenerator
from server.core.schema import BookMeta, CharacterCard
from server.core.store import NovelStore, Workbench


def _book(tmp_path: Path) -> NovelStore:
    return Workbench(tmp_path).create_novel(BookMeta(title="起名测试", genre="玄幻"))


# ---------------------------------------------------------------- 基本行为


def test_generate_all_kinds(tmp_path: Path):
    st = _book(tmp_path)
    g = NameGenerator(st)
    for kind in KIND_LABELS:
        pool = g.generate(kind=kind, count=6, seed=7, save=False)
        assert len(pool.candidates) == 6, kind
        assert all(c.name for c in pool.candidates), kind
        assert all(c.kind == kind for c in pool.candidates)


def test_unknown_kind_rejected(tmp_path: Path):
    g = NameGenerator(_book(tmp_path))
    with pytest.raises(ValueError) as ei:
        g.generate(kind="不存在的类别", count=3)
    assert "未知起名类别" in str(ei.value)


def test_count_is_clamped(tmp_path: Path):
    g = NameGenerator(_book(tmp_path))
    assert len(g.generate(count=0, save=False).candidates) == 1
    assert len(g.generate(count=999, save=False).candidates) == 50


# ---------------------------------------------------------------- 可复现


def test_same_seed_same_names(tmp_path: Path):
    """起名必须可复现：同一 seed 同一结果，否则作者的"挑好的名字"会漂。"""
    st = _book(tmp_path)
    g = NameGenerator(st)
    a = [c.name for c in g.generate(kind="skill", count=10, seed=99, save=False).candidates]
    b = [c.name for c in g.generate(kind="skill", count=10, seed=99, save=False).candidates]
    assert a == b
    c = [c.name for c in g.generate(kind="skill", count=10, seed=100, save=False).candidates]
    assert a != c, "换 seed 应该换结果"


# ---------------------------------------------------------------- 质量约束


def test_character_names_unique_within_batch(tmp_path: Path):
    """同一批不能出现两个一样的名字。"""
    g = NameGenerator(_book(tmp_path))
    pool = g.generate(kind="character", count=30, seed=2026, save=False)
    names = [c.name for c in pool.candidates]
    assert len(names) == len(set(names)) == 30


def test_character_names_are_two_or_three_chars(tmp_path: Path):
    """角色名只能是 2–3 字：出现过「穆北辰远」这种四字怪名（双字池拼双字名）。"""
    g = NameGenerator(_book(tmp_path))
    pool = g.generate(kind="character", count=50, seed=1, save=False)
    for c in pool.candidates:
        assert 2 <= len(c.name) <= 3, f"名字长度异常: {c.name}"


def test_generated_names_avoid_existing_characters(tmp_path: Path):
    """不能起出与已有角色同名的名字。"""
    st = _book(tmp_path)
    taken = CharacterCard(id="hero", name="林昭", role="主角")
    st.save_character(taken)

    g = NameGenerator(st)
    for seed in range(30):
        pool = g.generate(kind="character", count=10, seed=seed, save=False)
        assert all(c.name != "林昭" for c in pool.candidates)


def test_skill_names_follow_verb_imagery(tmp_path: Path):
    g = NameGenerator(_book(tmp_path))
    pool = g.generate(kind="skill", count=20, seed=5, save=False)
    for c in pool.candidates:
        assert len(c.name) == 2, c.name


def test_sect_names_have_org_suffix(tmp_path: Path):
    from server.agents.naming import SECT_SUFFIX

    g = NameGenerator(_book(tmp_path))
    pool = g.generate(kind="sect", count=20, seed=5, save=False)
    for c in pool.candidates:
        assert any(c.name.endswith(s) for s in SECT_SUFFIX), c.name


def test_place_names_have_terrain_suffix(tmp_path: Path):
    from server.agents.naming import PLACE_KIND

    g = NameGenerator(_book(tmp_path))
    pool = g.generate(kind="place", count=20, seed=5, save=False)
    for c in pool.candidates:
        assert any(c.name.endswith(s) for s in PLACE_KIND), c.name


def test_pools_have_no_duplicate_characters(tmp_path: Path):
    """用字池里不能有重复字，否则某些字出现频率翻倍且组合空间变窄。"""
    from server.agents.naming import GIVEN_FEMALE, GIVEN_MALE, GIVEN_NEUTRAL

    for name, pool in (("male", GIVEN_MALE), ("female", GIVEN_FEMALE), ("neutral", GIVEN_NEUTRAL)):
        assert len(pool) == len(set(pool)), f"{name} 池有重复字"


# ---------------------------------------------------------------- 落盘


def test_save_appends_to_names_pool(tmp_path: Path):
    st = _book(tmp_path)
    g = NameGenerator(st)
    g.generate(kind="character", count=3, seed=11, save=True)
    g.generate(kind="skill", count=2, seed=12, save=True)

    path = st.p.materials / "dictionary" / "names-pool.md"
    assert path.exists()
    text = path.read_text(encoding="utf-8")
    assert "# 起名词池" in text
    assert "| 名字 | 类别 | 备注 |" in text
    # 两次调用共 5 行数据
    rows = [l for l in text.splitlines() if l.startswith("| ") and "名字" not in l and "---" not in l]
    assert len(rows) == 5


def test_save_false_does_not_touch_disk(tmp_path: Path):
    st = _book(tmp_path)
    NameGenerator(st).generate(kind="character", count=3, seed=1, save=False)
    assert not (st.p.materials / "dictionary" / "names-pool.md").exists()


def test_read_pool_roundtrip(tmp_path: Path):
    st = _book(tmp_path)
    g = NameGenerator(st)
    pool = g.generate(kind="character", count=4, seed=3, save=True)
    rows = g.read_pool()
    assert [r["name"] for r in rows] == [c.name for c in pool.candidates]
    assert all(r["kind"] == "角色" for r in rows)


def test_names_pool_is_not_truth_source(tmp_path: Path):
    """names-pool 在 materials/ 下，不能被当作真相源参与一致性校验。"""
    st = _book(tmp_path)
    NameGenerator(st).generate(kind="character", count=2, seed=1, save=True)
    assert (st.p.materials / "dictionary" / "names-pool.md").exists()
    assert not (st.p.truth / "dictionary").exists()


def test_cli_name_command_registered():
    """§10.3 要求起名是 Planner 附属工具，CLI 必须有入口。"""
    from server.cli import build_parser

    p = build_parser()
    choices = set()
    for action in p._actions:
        if getattr(action, "choices", None) and isinstance(action.choices, dict):
            choices |= set(action.choices)
    assert "name" in choices
