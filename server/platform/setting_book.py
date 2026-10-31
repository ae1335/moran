"""设定集导出（M8）。

把一本书的真相源汇总成一份**人类可读的设定集**（Markdown + 可选的纯文本/JSON），
供投稿、给画师约稿、或者单纯存档用。

设计边界：
- **只读**：本模块不写 truth/，只读取并拼装。
- 产物入 `exports/{date}/setting-book-{date}.md`（与发布导出同目录，便于一起打包）。
- 章节正文明细可选（`include_chapters`）——设定集默认只放**设定**，不放大段正文。
- 缺文件不报错，段落标注「（暂无）」——设定集是给作者看的，不该因为某个文件没写就整体失败。
"""
from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass, field
from typing import Any

from ..core import atomic
from ..core.schema import ACTIVE_FORESHADOW_STATUSES, CharacterCard, ForeshadowBook
from ..core.store import NovelStore


@dataclass
class SettingBookResult:
    out_dir: str
    files: list[str]
    sections: list[str] = field(default_factory=list)
    words: int = 0
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "out_dir": self.out_dir,
            "files": self.files,
            "sections": self.sections,
            "words": self.words,
            "warnings": self.warnings,
        }


def _or_placeholder(text: str, placeholder: str = "（暂无）") -> str:
    return text.strip() if text and text.strip() else placeholder


_HEADING_RE = re.compile(r"^(#{1,6})\s", re.MULTILINE)


def _demote(text: str, levels: int = 2) -> str:
    """把嵌入文件的标题整体降级，避免书里的 `# 宪法` 截断设定集的 `## 分节` 层级。

    只处理行首的 ATX 标题；代码块内的 `#` 不误伤（按 ``` 围栏跳过）。
    """
    if not text:
        return text
    out: list[str] = []
    fenced = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
            out.append(line)
            continue
        if not fenced and _HEADING_RE.match(line):
            hashes = len(line) - len(line.lstrip("#"))
            new_level = min(6, hashes + levels)
            out.append("#" * new_level + line[hashes:])
        else:
            out.append(line)
    return "\n".join(out)


class SettingBookExporter:
    """设定集导出器。"""

    def __init__(self, store: NovelStore):
        self.store = store

    # ---------------- 各分节 ----------------
    def _section_overview(self, book) -> tuple[str, str]:
        target = book.target
        lo, hi = (target.words_per_chapter or (0, 0))[:2]
        words_per_ch = f"{lo}–{hi}" if hi else str(lo or "—")
        lines = [
            f"- **书名**：{book.title}",
            f"- **题材**：{book.genre or '（未设定）'}",
            f"- **平台**：{book.platform or '（未设定）'}",
            f"- **状态**：{book.status}",
            f"- **当前进度**：第 {book.current_chapter} 章",
            f"- **目标**：{words_per_ch} 字/章 · 计划 {target.total_chapters} 章 ·"
            f" 日更 {target.daily_chapters} 章",
        ]
        # 主角：book.protagonist 是角色卡路径（如 characters/mc.yaml），按卡读取
        hero = None
        if book.protagonist:
            hero = self.store.get_character(book.protagonist)
        if hero is None:
            hero = next(
                (c for c in self.store.list_characters() if c.role == "protagonist"), None
            )
        if hero is not None:
            lines += ["", "### 主角"]
            lines.append(f"- **姓名**：{hero.name}")
            if hero.personality:
                lines.append(f"- **性格**：{'、'.join(hero.personality)}")
            if hero.boundaries:
                lines.append(f"- **行为边界**：{'；'.join(hero.boundaries)}")
            ps = hero.power_scale
            if ps.rank or ps.ceiling:
                lines.append(f"- **境界**：{ps.rank or '?'} → 上限 {ps.ceiling or '?'}")
        return "作品概览", "\n".join(lines)

    def _section_characters(self) -> tuple[str, str]:
        cards = self.store.list_characters()
        if not cards:
            return "角色档案", "（暂无角色卡）"
        # 主角优先，其余按首次出场章排序
        cards = sorted(cards, key=lambda c: (0 if c.role == "protagonist" else 1, c.first_chapter, c.id))
        parts: list[str] = []
        for c in cards:
            parts.append(f"### {c.name}（`{c.id}`）")
            meta: list[str] = []
            if c.aliases:
                meta.append(f"别名：{'、'.join(c.aliases)}")
            if c.role:
                meta.append(f"定位：{c.role}")
            if c.status != "active":
                meta.append(f"状态：{c.status}")
            meta.append(f"出场：第 {c.first_chapter}–{c.last_chapter} 章")
            if meta:
                parts.append("> " + " · ".join(meta))
            parts.append("")
            if c.appearance:
                parts.append(f"- **外貌**：{c.appearance}")
            if c.personality:
                parts.append(f"- **性格**：{'、'.join(c.personality)}")
            if c.speech_habits:
                parts.append(f"- **说话习惯**：{'；'.join(c.speech_habits)}")
            if c.boundaries:
                parts.append(f"- **行为边界**：{'；'.join(c.boundaries)}")
            ps = c.power_scale
            if ps.rank or ps.ceiling or ps.forbidden:
                bits = [f"当前 {ps.rank or '?'}", f"上限 {ps.ceiling or '?'}"]
                if ps.forbidden:
                    bits.append(f"禁止：{ps.forbidden}")
                parts.append(f"- **战力**：{'；'.join(bits)}")
            if c.relations:
                rel = "；".join(
                    f"{r.target}（{r.type}{'：' + r.note if r.note else ''}）" for r in c.relations
                )
                parts.append(f"- **关系**：{rel}")
            parts.append("")
        return "角色档案", "\n".join(parts).strip()

    def _section_world(self) -> tuple[str, str]:
        names = self.store.list_world_files()
        if not names:
            return "世界观设定", "（暂无世界观文件）"
        parts: list[str] = []
        for name in names:
            text = self.store.read_truth_text(f"world/{name}")
            title = name.rsplit(".", 1)[0]
            parts.append(f"### {title}")
            parts.append(_demote(_or_placeholder(text)))
            parts.append("")
        return "世界观设定", "\n".join(parts).strip()

    def _section_constitution(self) -> tuple[str, str]:
        return "项目宪法与文风", "\n\n".join(
            [
                "### 项目宪法",
                _demote(_or_placeholder(self.store.read_truth_text("constitution.md"))),
                "### 文风指纹",
                _demote(_or_placeholder(self.store.read_truth_text("style-fingerprint.md"))),
            ]
        )

    def _section_arcs(self) -> tuple[str, str]:
        return "主线与弧线", "\n\n".join(
            [
                "### 总纲",
                _demote(_or_placeholder(self.store.read_truth_text("outline/master.md"))),
                "### 弧线与支线",
                _demote(_or_placeholder(self.store.read_truth_text("arcs.md"))),
            ]
        )

    def _section_threads(self) -> tuple[str, str]:
        threads = self.store.load_threads()
        if not threads:
            return "叙事线索", "（暂无线索）"
        rows = ["| 线索 | 类型 | 状态 | 进度 | 最近章 |", "|---|---|---|---|---|"]
        for t in threads:
            rows.append(
                f"| {t.name} | {t.kind} | {t.status} | {t.progress or '—'} | {t.last_ch or '—'} |"
            )
        beats: list[str] = []
        for t in threads:
            if t.beats:
                beats.append(f"- **{t.name}**：" + "；".join(t.beats[:8]))
        out = "\n".join(rows)
        if beats:
            out += "\n\n**关键节点**\n" + "\n".join(beats)
        return "叙事线索", out

    def _section_foreshadow(self, current_ch: int) -> tuple[str, str]:
        fb = self.store.load_foreshadow()
        if not fb.items:
            return "伏笔台账", "（暂无伏笔）"
        active = [i for i in fb.items if i.status in ACTIVE_FORESHADOW_STATUSES]
        resolved = [i for i in fb.items if i.status == "resolved"]
        overdue = [i for i in fb.items if i.is_overdue(current_ch)]
        lines = [
            f"- 共 {len(fb.items)} 条：进行中 {len(active)} · 已回收 {len(resolved)} · 超期 {len(overdue)}",
            "",
            "| ID | 描述 | 状态 | 埋设章 | 回收/截止 |",
            "|---|---|---|---|---|",
        ]
        for it in sorted(fb.items, key=lambda x: (x.planted_ch or 0, x.id)):
            mark = " ⚠️超期" if it.is_overdue(current_ch) else ""
            tail = (
                f"已回收 第{it.resolution_ch}章"
                if it.resolution_ch
                else (f"截止 第{it.deadline_ch}章" if it.deadline_ch else "—")
            )
            lines.append(f"| {it.id} | {it.desc} | {it.status}{mark} | {it.planted_ch or '—'} | {tail} |")
        if overdue:
            lines += ["", "### 超期预警"]
            for it in overdue:
                lines.append(f"- **{it.id}** {it.desc}（截止第 {it.deadline_ch} 章，已超 {current_ch - (it.deadline_ch or 0)} 章）")
        return "伏笔台账", "\n".join(lines)

    def _section_timeline(self) -> tuple[str, str]:
        entries = self.store.load_timeline()
        if not entries:
            return "时间线", "（暂无时间线记录）"
        rows = ["| 章 | 虚构日期 | 事件 |", "|---|---|---|"]
        for e in entries:
            rows.append(f"| {e.ch} | {e.date} | {e.event} |")
        return "时间线", "\n".join(rows)

    def _section_resources(self) -> tuple[str, str]:
        raw = self.store.read_truth_text("resources.json")
        if not raw.strip():
            try:
                raw = json.dumps(
                    json.loads(atomic.read_text(self.store.p.resources) or "{}"),
                    ensure_ascii=False,
                    indent=2,
                )
            except Exception:
                return "资源与道具", "（暂无资源台账）"
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return "资源与道具", "（资源台账格式异常，已跳过）"
        held = data.get("held") or []
        if not held:
            return "资源与道具", "（暂无持有物）"
        rows = ["| 物品 | 说明 | 获得章 |", "|---|---|---|"]
        for it in held:
            if isinstance(it, dict):
                rows.append(
                    f"| {it.get('name') or it.get('id') or '—'} | {it.get('note') or it.get('desc') or '—'} | {it.get('ch') or '—'} |"
                )
            else:
                rows.append(f"| {it} | — | — |")
        return "资源与道具", "\n".join(rows)

    def _section_outlines(self, limit: int = 0) -> tuple[str, str]:
        chs = self.store.list_outlines()
        if not chs:
            return "章纲索引", "（暂无章纲）"
        rows = ["| 章 | 标题 | 状态 | 目标字数 | 钩子 |", "|---|---|---|---|---|"]
        shown = chs[:limit] if limit else chs
        for o in shown:
            hook = (o.hook or "").replace("\n", " ")[:40]
            rows.append(f"| {o.ch} | {o.title} | {o.status} | {o.target_words} | {hook or '—'} |")
        out = "\n".join(rows)
        if limit and len(chs) > limit:
            out += f"\n\n（共 {len(chs)} 章，此处仅列前 {limit} 章）"
        return "章纲索引", out

    def _section_chapter_index(self, include_chapters: bool) -> tuple[str, str]:
        nums = self.store.list_chapters()
        if not nums:
            return "章节清单", "（暂无正文）"
        rows = ["| 章 | 标题 | 字数 |", "|---|---|---|"]
        total = 0
        for ch in nums:
            o = self.store.load_outline(ch)
            title = o.title if o else ""
            w = self.store.chapter_word_count(ch)
            total += w
            rows.append(f"| {ch} | {title or '—'} | {w:,} |")
        head = f"共 {len(nums)} 章，累计 {total:,} 字。\n\n"
        if not include_chapters:
            return "章节清单", head + "\n".join(rows)
        # 含正文时按章附上（设定集默认不含，避免文件过大）
        parts = [head + "\n".join(rows), ""]
        for ch in nums:
            text = self.store.read_chapter(ch)
            if not text.strip():
                continue
            o = self.store.load_outline(ch)
            parts.append(f"---\n\n## 第 {ch} 章 {o.title if o else ''}".rstrip())
            parts.append("")
            parts.append(text.strip())
            parts.append("")
        return "章节清单", "\n".join(parts)

    # ---------------- 主入口 ----------------
    def export(
        self,
        *,
        include_chapters: bool = False,
        outline_limit: int = 100,
        dry_run: bool = False,
    ) -> SettingBookResult:
        book = self.store.load_book()
        current_ch = book.current_chapter or 0

        sections: list[tuple[str, str]] = [
            self._section_overview(book),
            self._section_constitution(),
            self._section_world(),
            self._section_characters(),
            self._section_threads(),
            self._section_foreshadow(current_ch),
            self._section_timeline(),
            self._section_resources(),
            self._section_arcs(),
            self._section_outlines(outline_limit),
            self._section_chapter_index(include_chapters),
        ]

        today = dt.date.today().isoformat()
        lines = [
            f"# 《{book.title}》设定集",
            "",
            f"> 由网文工作台自动汇总 · {today} · 共 {len(sections)} 节",
            "",
            "## 目录",
            "",
        ]
        for i, (title, _) in enumerate(sections, 1):
            lines.append(f"{i}. {title}")
        lines.append("")
        for title, body in sections:
            lines += ["---", "", f"## {title}", "", body or "（暂无）", ""]

        content = "\n".join(lines)
        warnings: list[str] = []
        empty = [t for t, b in sections if not b.strip() or b.strip() == "（暂无）"]
        if empty:
            warnings.append(f"以下分节内容为空：{'、'.join(empty)}")

        words = len(content)
        if dry_run:
            return SettingBookResult(
                out_dir="", files=[], sections=[t for t, _ in sections], words=words, warnings=warnings
            )

        out_dir = self.store.p.exports / today
        out_dir.mkdir(parents=True, exist_ok=True)
        md_path = out_dir / f"setting-book-{today}.md"
        atomic.write_text_atomic(md_path, content)

        files = [self.store.rel(md_path)]
        # 附一份 JSON 摘要，便于程序二次消费（不含正文明细，避免重复存正文）
        summary = {
            "title": book.title,
            "genre": book.genre,
            "platform": book.platform,
            "current_chapter": current_ch,
            "characters": [
                {
                    "id": c.id,
                    "name": c.name,
                    "role": c.role,
                    "aliases": c.aliases,
                    "power_scale": c.power_scale.model_dump(mode="json"),
                }
                for c in self.store.list_characters()
            ],
            "foreshadow": {
                "total": len(fb.items) if (fb := self.store.load_foreshadow()) else 0,
                "active": len([i for i in fb.items if i.status in ACTIVE_FORESHADOW_STATUSES]) if fb else 0,
            },
            "timeline": len(self.store.load_timeline()),
            "chapters": len(self.store.list_chapters()),
            "generated": dt.datetime.now().isoformat(timespec="seconds"),
        }
        json_path = out_dir / f"setting-book-{today}.json"
        atomic.write_json_atomic(json_path, summary)
        files.append(self.store.rel(json_path))

        return SettingBookResult(
            out_dir=self.store.rel(out_dir),
            files=files,
            sections=[t for t, _ in sections],
            words=words,
            warnings=warnings,
        )
