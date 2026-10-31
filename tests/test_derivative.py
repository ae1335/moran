"""M8 衍生层测试：题材模板 / 设定集导出 / 封面生成。"""
from __future__ import annotations

import json

import pytest

from server.core.schema import BookMeta, CharacterCard
from server.core.store import Workbench
from server.core.templates import (
    GenreTemplate,
    TemplateLibrary,
    parse_template,
)
from server.platform.cover import CoverGenerator, build_prompt, theme_for
from server.platform.setting_book import SettingBookExporter, _demote


@pytest.fixture()
def wb(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    w = Workbench(root)
    w.paths.ensure()
    return w


@pytest.fixture()
def book(wb):
    return wb.create_novel(BookMeta(title="测试书", genre="玄幻-东方玄幻"))


# ---------------------------------------------------------------- 模板解析
def test_parse_template_reads_nested_structure():
    raw = {
        "structure": {"genre": "仙侠", "opening_pattern": "废材开局", "arc_length_ch": "40"},
        "pacing": {"coolpoint_interval_ch": 2},
        "golden_finger": "吞灵鼎",
        "source": "bench-a",
    }
    tpl = parse_template(raw, id="bench-a")
    assert tpl.genre == "仙侠"
    assert tpl.opening_pattern == "废材开局"
    assert tpl.arc_length_ch == 40
    assert tpl.coolpoint_interval_ch == 2
    assert tpl.golden_finger == "吞灵鼎"


def test_parse_template_tolerates_garbage():
    """脏数据不抛——拆书产物可能字段缺失或类型错。"""
    tpl = parse_template({"arc_length_ch": "不是数字", "genre": None}, id="x")
    assert tpl.arc_length_ch == 0
    assert tpl.genre == ""


def test_template_brief_skips_empty_fields():
    tpl = GenreTemplate(id="a", opening_pattern="废材逆袭", arc_length_ch=40)
    brief = tpl.brief()
    assert "开局模式：废材逆袭" in brief
    assert "单卷章数基线：40" in brief
    assert "金手指" not in brief


def test_template_library_shared_overrides_book(wb, book):
    """同 id 时工作区共享库优先（跨书共享语义）。"""
    lib = book.workbench_template_library()
    lib.shared_dir.mkdir(parents=True, exist_ok=True)
    book_dir = book.p.root / "analysis" / "templates"
    book_dir.mkdir(parents=True, exist_ok=True)
    (book_dir / "same.json").write_text(json.dumps({"genre": "书内镜像"}), encoding="utf-8")
    (lib.shared_dir / "same.json").write_text(json.dumps({"genre": "共享库"}), encoding="utf-8")
    found = lib.get("same")
    assert found is not None
    assert found.genre == "共享库"


def test_template_library_match_genre_prefix(wb, book):
    lib = book.workbench_template_library()
    lib.save(parse_template({"genre": "玄幻-东方玄幻", "opening_pattern": "开局A"}, id="t1"))
    hit = lib.match_genre("玄幻-东方玄幻-升级流")
    assert hit is not None and hit.id == "t1"
    assert lib.match_genre("都市异能") is None


# ---------------------------------------------------------------- 建书套模板
def test_create_novel_applies_template_to_master_outline(wb):
    lib = TemplateLibrary(wb.paths.shared_templates)
    lib.save(parse_template({"opening_pattern": "废材逆袭", "golden_finger": "吞灵鼎"}, id="tpl-a"))
    store = wb.create_novel(BookMeta(title="套模板书", genre="玄幻"), template="tpl-a")
    text = store.p.master_outline.read_text(encoding="utf-8")
    assert "废材逆袭" in text
    assert "吞灵鼎" in text
    assert "题材模板基线" in text


def test_create_novel_applies_template_to_volume_outline(wb):
    lib = TemplateLibrary(wb.paths.shared_templates)
    lib.save(parse_template({"power_up_interval_ch": 8, "coolpoint_interval_ch": 2}, id="tpl-b"))
    store = wb.create_novel(BookMeta(title="节奏书", genre="玄幻"), template="tpl-b")
    text = store.p.volume_outline(1).read_text(encoding="utf-8")
    assert "约每 8 章一次" in text
    assert "不超过 2 章" in text


def test_create_novel_auto_matches_by_genre(wb):
    lib = TemplateLibrary(wb.paths.shared_templates)
    lib.save(parse_template({"genre": "仙侠-修真", "golden_finger": "剑胎"}, id="xianxia"))
    store = wb.create_novel(BookMeta(title="自动匹配书", genre="仙侠-修真-门派流"))
    assert "剑胎" in store.p.master_outline.read_text(encoding="utf-8")


def test_create_novel_without_template_uses_builtin_seed(wb):
    """没有模板时必须仍能建书（模板是增强不是依赖）。"""
    store = wb.create_novel(BookMeta(title="裸书", genre="未知题材"))
    text = store.p.master_outline.read_text(encoding="utf-8")
    assert "三幕结构" in text
    assert "题材模板基线" not in text


def test_create_novel_bad_template_id_falls_back(wb):
    """传了不存在的模板 id：不报错，回落内置种子。"""
    store = wb.create_novel(BookMeta(title="坏模板书", genre="玄幻"), template="不存在的模板")
    assert store.p.master_outline.exists()


# ---------------------------------------------------------------- 设定集
def test_setting_book_dry_run_has_all_sections(book):
    res = SettingBookExporter(book).export(dry_run=True)
    assert len(res.sections) == 11
    assert "角色档案" in res.sections
    assert "伏笔台账" in res.sections
    assert res.words > 0
    assert res.files == []


def test_setting_book_writes_md_and_json(book):
    res = SettingBookExporter(book).export()
    assert len(res.files) == 2
    md = (book.root / res.files[0]).read_text(encoding="utf-8")
    assert md.startswith("# 《测试书》设定集")
    assert "## 作品概览" in md
    data = json.loads((book.root / res.files[1]).read_text(encoding="utf-8"))
    assert data["title"] == "测试书"


def test_setting_book_heading_hierarchy(book):
    """嵌入的 `# 宪法` 必须被降级，否则设定集的 `## 分节` 层级被打断。"""
    book.p.constitution.write_text("# 某书宪法\n\n## 红线\n内容\n", encoding="utf-8")
    res = SettingBookExporter(book).export()
    md = (book.root / res.files[0]).read_text(encoding="utf-8")
    # 全文一级标题只有一个（书名）
    assert md.count("\n# ") == 0
    assert md.startswith("# 《测试书》设定集")
    assert "### 某书宪法" in md


def test_demote_skips_fenced_code():
    text = "# 标题\n```\n# 这是代码里的井号\n```\n## 二级"
    out = _demote(text, 2)
    assert "### 标题" in out
    assert "# 这是代码里的井号" in out  # 代码块内不动
    assert "#### 二级" in out


def test_setting_book_includes_characters(book):
    book.save_character(
        CharacterCard(
            id="hero",
            name="林澈",
            role="protagonist",
            personality=["外冷内热"],
            power_scale={"rank": "淬体七重", "ceiling": "淬体九重"},
        )
    )
    res = SettingBookExporter(book).export()
    md = (book.root / res.files[0]).read_text(encoding="utf-8")
    assert "林澈" in md
    assert "外冷内热" in md
    assert "淬体七重" in md


def test_setting_book_empty_book_still_succeeds(wb):
    """空书也要能导出（设定集不该因为某文件没写就整体失败）。"""
    store = wb.create_novel(BookMeta(title="空书", genre="玄幻"))
    res = SettingBookExporter(store).export()
    assert res.files
    assert "（暂无" in (store.root / res.files[0]).read_text(encoding="utf-8")


# ---------------------------------------------------------------- 封面
def test_theme_matches_genre():
    assert theme_for("玄幻-东方玄幻")["accent"] == "#c9a227"
    assert theme_for("科幻-太空")["accent"] == "#00d4ff"
    assert theme_for("不存在的题材")["accent"] == "#c9a227"  # 默认


def test_cover_svg_is_valid_xml(book):
    import xml.etree.ElementTree as ET

    svg = CoverGenerator(book).render_svg(600, 800)
    root = ET.fromstring(svg)
    assert root.tag.endswith("svg")
    assert root.get("width") == "600"


def test_cover_escapes_title(book):
    """书名带 & < 时必须转义，否则 SVG 直接打不开。"""
    book.load_book()
    meta = book.load_book()
    meta.title = "剑&影<上>"
    book.save_book(meta)
    svg = CoverGenerator(book).render_svg()
    assert "&amp;" in svg
    assert "&lt;" in svg
    import xml.etree.ElementTree as ET

    ET.fromstring(svg)  # 能解析 = 没打坏


def test_cover_generate_saves_file(book):
    res = CoverGenerator(book).generate()
    assert res.path.startswith("exports/covers/")
    assert res.format == "svg"
    saved = (book.root / res.path).read_text(encoding="utf-8")
    assert saved.startswith("<svg")


def test_cover_prompt_mentions_title_and_genre(book):
    prompt = build_prompt(book)
    assert "测试书" in prompt
    assert "玄幻" in prompt


def test_cover_image_api_falls_back_with_warning(book):
    """请求图像 API 但没适配器时：回退 SVG 并明确告警，不静默。"""
    res = CoverGenerator(book, image_api={"endpoint": "http://127.0.0.1:9999"}).generate(
        use_image_api=True
    )
    assert res.format == "svg"
    assert any("回退" in w for w in res.warnings)


def test_cover_generate_without_save(book):
    res = CoverGenerator(book).generate(save=False)
    assert res.path == ""
    assert not (book.p.exports / "covers").exists()
