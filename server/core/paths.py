"""目录结构与路径约定（§4.1）。

一本书 = 一个 git 仓库。真相源全部是文件，路径规则写死在这里，
任何模块都不得自行拼接目录，必须走 NovelPaths。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

# 章节文件名：ch-0042（四位零填充）
CH_FMT = "ch-{:04d}"
CH_RE = re.compile(r"^ch-(\d{4,})\.md$")
CH_JSON_RE = re.compile(r"^ch-(\d{4,})\.json$")

UNIT_DIRS = (
    "truth",
    "truth/characters",
    "truth/world",
    "outline",
    "outline/threads",
    "outline/chapters",
    "chapters",
    "memory",
    "analysis",
    "analysis/benchmarks",
    "analysis/templates",
    "materials",
    "materials/snippets",
    "materials/snippets/combat",
    "materials/snippets/emotion",
    "materials/snippets/scene",
    "materials/snippets/dialogue",
    "materials/dictionary",
    "feedback",
    "feedback/reviews",
    "profiles",
    "exports",
    ".rag-index",
)


# 缺陷 #60：章号上界。ch_name 只做格式化不做校验，1e9 会写出 ch-1000000000.md，
# 而章号同时是章纲/审稿/检索的表主键，越界的章号不会报错只会写出没人认领的文件。
MAX_CHAPTER_NO = 100_000


def ch_name(ch: int) -> str:
    """42 -> 'ch-0042'"""
    return CH_FMT.format(ch)


def _assert_chapter_no(ch: int) -> int:
    """章号合理上界（缺陷 #60）。越界直接 ValueError，不让越界文件落到盘上。"""
    if not isinstance(ch, int) or isinstance(ch, bool):
        raise ValueError(f"章号必须是整数: {ch!r}")
    if ch < 1:
        raise ValueError(f"章号必须为正: {ch}")
    if ch > MAX_CHAPTER_NO:
        raise ValueError(f"章号超出范围：{ch}（上限 {MAX_CHAPTER_NO}）")
    return ch



# 缺陷 #60：书名长度上界。ext4 单文件名上限 255 字节，而中文 UTF-8 是 3 字节/字，
# 目录名还要带上 novel-《》 与书内各子路径，60 字（约 192 字节）是安全线。
# 超长书名会在建目录那一刻炸 OSError(Errno 36)，而且是在 HTTP 层变成 500，
# 作者看到的是一句看不懂的英文——必须在进到文件系统之前就挡掉。
MAX_TITLE_CHARS = 60


def safe_title(title: str) -> str:
    """书名 -> 可做目录名的字符串。保留中文，剔除路径分隔符与非法字符。"""
    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", title.strip())
    cleaned = cleaned.replace("..", "_").strip(". ")
    if len(cleaned) > MAX_TITLE_CHARS:
        raise ValueError(f"书名过长：{len(cleaned)} 字（上限 {MAX_TITLE_CHARS} 字）")
    return cleaned or "untitled"



def novel_dirname(title: str) -> str:
    """《书名》 -> 'novel-《书名》'"""
    return f"novel-《{safe_title(title)}》"


@dataclass(frozen=True)
class NovelPaths:
    """一本书的路径集合。所有 IO 必须从这里取路径。"""

    root: Path

    # ---- 元信息 ----
    @property
    def book_json(self) -> Path:
        return self.root / "book.json"

    # ---- truth/ ----
    @property
    def truth(self) -> Path:
        return self.root / "truth"

    @property
    def constitution(self) -> Path:
        return self.truth / "constitution.md"

    @property
    def characters_dir(self) -> Path:
        return self.truth / "characters"

    @property
    def world_dir(self) -> Path:
        return self.truth / "world"

    def world_file(self, name: str) -> Path:
        return self.world_dir / name

    @property
    def timeline(self) -> Path:
        return self.truth / "timeline.yaml"

    @property
    def world_state(self) -> Path:
        return self.truth / "world-state.md"

    @property
    def foreshadow(self) -> Path:
        return self.truth / "foreshadow.json"

    @property
    def resources(self) -> Path:
        return self.truth / "resources.json"

    @property
    def arcs(self) -> Path:
        return self.truth / "arcs.md"

    @property
    def style_fingerprint(self) -> Path:
        return self.truth / "style-fingerprint.md"

    # ---- outline/ ----
    @property
    def outline(self) -> Path:
        return self.root / "outline"

    @property
    def master_outline(self) -> Path:
        return self.outline / "master.md"

    @property
    def threads_dir(self) -> Path:
        return self.outline / "threads"

    @property
    def chapters_outline_dir(self) -> Path:
        return self.outline / "chapters"

    def outline_json(self, ch: int) -> Path:
        return self.chapters_outline_dir / f"{ch_name(_assert_chapter_no(ch))}.json"

    def volume_outline(self, vol: int) -> Path:
        return self.outline / f"volume-{vol:02d}.md"

    # ---- chapters/ ----
    @property
    def chapters_dir(self) -> Path:
        return self.root / "chapters"

    def chapter_md(self, ch: int) -> Path:
        return self.chapters_dir / f"{ch_name(_assert_chapter_no(ch))}.md"

    def chapter_versions_dir(self, ch: int) -> Path:
        return self.chapters_dir / f"{ch_name(_assert_chapter_no(ch))}.versions"

    def chapter_version(self, ch: int, v: int) -> Path:
        return self.chapter_versions_dir(ch) / f"v{v}.md"

    def chapter_version_meta(self, ch: int, v: int) -> Path:
        return self.chapter_versions_dir(ch) / f"v{v}.meta.json"

    # ---- memory/ ----
    @property
    def memory(self) -> Path:
        return self.root / "memory"

    def summary_md(self, ch: int) -> Path:
        return self.memory / f"{ch_name(_assert_chapter_no(ch))}.summary.md"

    def assembled_md(self, ch: int) -> Path:
        return self.memory / f"{ch_name(_assert_chapter_no(ch))}.assembled.md"

    def review_json(self, ch: int) -> Path:
        return self.memory / f"{ch_name(_assert_chapter_no(ch))}.review.json"

    @property
    def entity_deltas(self) -> Path:
        return self.memory / "entity-deltas.jsonl"

    # ---- 其它 ----
    @property
    def materials(self) -> Path:
        return self.root / "materials"

    @property
    def feedback(self) -> Path:
        return self.root / "feedback"

    @property
    def feedback_stats(self) -> Path:
        return self.feedback / "stats.json"

    def feedback_review(self, ch: int) -> Path:
        return self.feedback / "reviews" / f"{ch_name(ch)}.md"

    @property
    def profiles(self) -> Path:
        return self.root / "profiles"

    @property
    def exports(self) -> Path:
        return self.root / "exports"

    @property
    def rag_index(self) -> Path:
        return self.root / ".rag-index"

    # ---- 遍历 ----
    def iter_chapter_numbers(self) -> Iterator[int]:
        """已存在的正文章节号（升序）。"""
        if not self.chapters_dir.exists():
            return
        nums = []
        for p in self.chapters_dir.iterdir():
            m = CH_RE.match(p.name)
            if m:
                nums.append(int(m.group(1)))
        yield from sorted(nums)

    def iter_outline_numbers(self) -> Iterator[int]:
        if not self.chapters_outline_dir.exists():
            return
        nums = []
        for p in self.chapters_outline_dir.iterdir():
            m = CH_JSON_RE.match(p.name)
            if m:
                nums.append(int(m.group(1)))
        yield from sorted(nums)

    def iter_character_files(self) -> Iterator[Path]:
        if not self.characters_dir.exists():
            return
        for p in sorted(self.characters_dir.iterdir()):
            if p.suffix in (".yaml", ".yml"):
                yield p

    def rel(self, path: Path) -> str:
        """返回相对书根的 POSIX 路径（真相源里的引用都用这种形式）。"""
        try:
            return path.resolve().relative_to(self.root.resolve()).as_posix()
        except ValueError:
            return path.as_posix()


@dataclass(frozen=True)
class WorkbenchPaths:
    """工作区根（跨书共享）。"""

    root: Path

    @property
    def novels(self) -> Path:
        return self.root / "novels"

    @property
    def shelf(self) -> Path:
        return self.root / "workbench" / "shelf.json"

    @property
    def workbench(self) -> Path:
        return self.root / "workbench"

    @property
    def shared_materials(self) -> Path:
        return self.workbench / "materials"

    @property
    def shared_templates(self) -> Path:
        return self.workbench / "analysis" / "templates"

    @property
    def config_dir(self) -> Path:
        return self.root / "workbench-config"

    @property
    def providers_yaml(self) -> Path:
        return self.config_dir / "providers.yaml"

    @property
    def routing_yaml(self) -> Path:
        return self.config_dir / "routing.yaml"

    @property
    def env_file(self) -> Path:
        return self.config_dir / ".env"

    @property
    def db_path(self) -> Path:
        return self.root / "workbench" / "workbench.db"

    def novel(self, dirname: str) -> NovelPaths:
        return NovelPaths(self.novels / dirname)

    def ensure(self) -> None:
        for p in (self.novels, self.workbench, self.shared_materials, self.shared_templates, self.config_dir):
            p.mkdir(parents=True, exist_ok=True)


def ensure_novel_dirs(np: NovelPaths) -> None:
    """创建一本书的标准目录骨架。幂等。"""
    for rel in UNIT_DIRS:
        (np.root / rel).mkdir(parents=True, exist_ok=True)
