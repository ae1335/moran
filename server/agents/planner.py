"""Planner（§6.1）：总纲 + 卷纲 + threads + 报警 → 章纲草案批次。

产物是**草案**，作者确认后才进入 writing 状态。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..core import atomic
from ..core.domain import (
    can_outline_transition,
    overdue_alerts,
    validate_foreshadow_plan,
    validate_power_events,
)
from ..core.schema import ChapterOutline
from ..core.store import NovelStore
from ..model.gateway import ModelGateway
from .prompts import character_brief, parse_chapters, planner_prompt, PLANNER_SYSTEM, threads_block


@dataclass
class PlanResult:
    chapters: list[ChapterOutline]
    warnings: list[str] = field(default_factory=list)
    alerts: list[dict[str, Any]] = field(default_factory=list)
    model: str = ""
    saved: int = 0
    rewritten: list[dict[str, str]] = field(default_factory=list)


def draft_status(src: str) -> str:
    """规划落盘时的目标状态：新章是 draft，已有章降到 drafted 而不是倒回 draft。

    缺陷 #73：重跑规划不应该把一章拉回比它现在更「早」的状态。
    planned / writing / drafted 的章保持 drafted——既表示「有新草案待确认」，
    又不越过状态机（drafted 回 planned 是合法迁移，published 回 draft 不是）。
    """
    if src in ("planned", "writing", "drafted"):
        return "drafted"
    return "draft"


class Planner:
    def __init__(self, store: NovelStore, gateway: ModelGateway):
        self.store = store
        self.gw = gateway

    async def plan(
        self,
        *,
        start_ch: int | None = None,
        count: int = 5,
        save: bool = True,
        model_note: str = "",
    ) -> PlanResult:
        book = self.store.load_book()
        chaps = self.store.list_chapters()
        outlines = self.store.list_outlines()
        last_ch = max(
            [book.current_chapter, *chaps, *(o.ch for o in outlines)] or [0]
        )
        start = start_ch or (last_ch + 1)
        count = max(1, min(count, 20))

        master = self._read_optional(self.store.p.master_outline)
        volume = self._read_optional(self.store.p.volume_outline(1))
        world_state = self._read_optional(self.store.p.world_state)
        style = self._read_optional(self.store.p.style_fingerprint)
        # 避雷区是反馈复盘的关键产物，单独提取；不能被文风全文截断挤掉（§12.2）
        avoid = self._avoidance_brief(style)
        if avoid:
            style = f"{style[:1200]}\n\n{avoid}"
        threads = [t.model_dump(mode="json") for t in self.store.load_threads()]
        cards = self.store.list_characters()
        fb = self.store.load_foreshadow()
        alerts = overdue_alerts(fb, start - 1, horizon=count + 5)

        # 拆书节奏基线（若已有对标）
        pacing = self._pacing_brief()
        # 题材模板基线（§10.2：提炼后的结构化节奏参数）
        tpl_brief = self._template_brief()
        if tpl_brief:
            pacing = f"{tpl_brief}\n\n{pacing}" if pacing else tpl_brief

        prompt = planner_prompt(
            title=book.title,
            genre=book.genre,
            start_ch=start,
            count=count,
            master=master,
            volume=volume,
            threads=threads,
            world_state=world_state,
            character_brief=character_brief(cards),
            foreshadow_brief=self._foreshadow_brief(),
            overdue_brief=self._overdue_brief(alerts),
            pacing_brief=pacing,
            style_brief=style,
            target_words=book.target.words_per_chapter,
        )

        payload = await self.gw.complete_json("plan", prompt, system=PLANNER_SYSTEM, max_tokens=6000)
        chapters = parse_chapters(payload)
        if not chapters:
            raise ValueError("Planner 返回的章纲为空，请检查模型输出或降低要求")

        # 章号连续性修正（模型常跳号）
        warnings: list[str] = []
        for i, o in enumerate(chapters):
            expect = start + i
            if o.ch != expect:
                warnings.append(f"章号修正：模型给 {o.ch}，强制为 {expect}")
                o.ch = expect

        # 校验：伏笔 / 战力
        char_map = self.store.character_map()
        for o in chapters:
            for issue in validate_foreshadow_plan(fb, o):
                warnings.append(f"第{o.ch}章伏笔：{issue.message}")
            for pi in validate_power_events(o, char_map):
                warnings.append(f"第{o.ch}章战力：{pi.message}")

        saved = 0
        rewritten: list[dict[str, str]] = []
        if save:
            for o in chapters:
                existing = self.store.load_outline(o.ch)
                if existing and existing.status in ("final", "reviewed"):
                    warnings.append(f"第{o.ch}章已是 {existing.status}，跳过覆盖")
                    continue
                # 缺陷 #73：原先无条件把状态写成 draft。草稿/在写还算早，但
                # published 章被倒回会让 daily-panel 把已发布章算进存稿：
                # 读者端已经看过的章节在作者端凭空变成未发布。终态必须拦住。
                if existing and existing.status == "published":
                    warnings.append(
                        f"第{o.ch}章已发布，跳过覆盖（已发布章不能倒回草案）"
                    )
                    continue
                o.status = draft_status(existing.status if existing else "draft")
                self.store.save_outline(o)
                saved += 1
                if existing and existing.status != o.status:
                    rewritten.append(
                        {"ch": str(o.ch), "from": existing.status, "to": o.status}
                    )

        return PlanResult(
            chapters=chapters,
            warnings=warnings,
            alerts=alerts,
            model=model_note,
            saved=saved,
            rewritten=rewritten,
        )

    # ---------------- 内部 ----------------
    @staticmethod
    def _read_optional(path) -> str:
        try:
            return path.read_text(encoding="utf-8")
        except Exception:
            return ""

    def _foreshadow_brief(self) -> str:
        fb = self.store.load_foreshadow()
        if not fb.items:
            return ""
        lines = []
        for it in fb.items:
            lines.append(f"- {it.id}：{it.desc}（{it.status}，埋于 {it.planted_ch}，截止 {it.deadline_ch}）")
        return "\n".join(lines)

    @staticmethod
    def _overdue_brief(alerts: list[dict[str, Any]]) -> str:
        if not alerts:
            return ""
        lines = []
        for a in alerts:
            lines.append(f"- [{a['level']}] {a['id']} {a['desc']}：{a.get('suggestion', '')}")
        return "\n".join(lines)

    @staticmethod
    def _avoidance_brief(style: str) -> str:
        import re

        if not style:
            return ""
        match = re.search(r"^##\s+本章避雷\s*$", style, re.MULTILINE)
        if not match:
            return ""
        tail = style[match.end():]
        nxt = re.search(r"^##\s+", tail, re.MULTILINE)
        body = (tail[: nxt.start()] if nxt else tail).strip()
        entries = [line for line in body.splitlines() if line.strip() and "（暂无" not in line]
        if not entries:
            return ""
        # 最多取最近 12 条，给模型留出章纲空间
        return "## 反馈复盘避雷（必须落实到后续章纲）\n" + "\n".join(entries[-12:])

    def _pacing_brief(self) -> str:
        """从 analysis/benchmarks 读节奏基线（§10.2）。"""
        base = self.store.p.root / "analysis" / "benchmarks"
        if not base.exists():
            return ""
        parts: list[str] = []
        for d in sorted(base.iterdir()):
            if not d.is_dir():
                continue
            hooks = d / "hooks.md"
            pacing = d / "pacing.md"
            # 缺陷 #91：产物目录里的二进制/非 UTF-8 文件不该让整条规划失败
            hooks_txt = atomic.read_text(hooks, None) if hooks.exists() else None
            pacing_txt = atomic.read_text(pacing, None) if pacing.exists() else None
            if hooks_txt:
                parts.append(f"## 对标书《{d.name}》钩子模式\n{hooks_txt[:800]}")
            if pacing_txt:
                parts.append(f"## 对标书《{d.name}》节奏\n{pacing_txt[:800]}")
        return "\n\n".join(parts)

    def _template_brief(self) -> str:
        """题材模板基线（§10.2）：书内镜像 + 工作区共享库。

        与 `_pacing_brief` 的区别：那份是**逐本的拆书产物**（pacing.md/hooks.md），
        这份是**提炼后的模板参数**（开局模式/金手指/升级间隔/爽点间隔），
        章纲生成时按结构化基线注入。都没有时返回空串，不影响原有行为。
        """
        try:
            lib = self.store.workbench_template_library() if hasattr(
                self.store, "workbench_template_library"
            ) else None
        except Exception:
            lib = None
        if lib is None:
            return ""
        try:
            tpls = [t for t in lib.list() if t.id]
        except Exception:
            return ""
        if not tpls:
            return ""
        lines = []
        for t in tpls[:3]:  # 最多 3 份，避免 prompt 膨胀
            brief = t.brief()
            if brief:
                lines.append(f"- 《{t.id}》：{brief}")
        if not lines:
            return ""
        return "## 题材模板基线（来自拆书，仅学节奏不抄情节）\n" + "\n".join(lines)


def confirm_outline(store: NovelStore, ch: int, *, status: str = "planned") -> ChapterOutline:
    """作者确认章纲：draft → planned（§6.1 作者环节）。

    缺陷 #72：这里原先是无条件赋值，连 published / final 都能被拉回 planned。
    「确认」的语义是向前推进，不是把终态倒回去——已发布或已定稿的章被拉回
    planned，会让 daily-panel 的存稿口径凭空多出一章（published 被排除出 stock）。
    """
    o = store.load_outline(ch)
    if o is None:
        raise ValueError(f"第 {ch} 章章纲不存在")
    src = str(o.status or "draft")
    dst = str(status)
    if dst != src and not can_outline_transition(src, dst):
        raise ValueError(
            f"非法状态迁移：{src} → {dst}"
            "（章纲状态只能按 draft → planned → writing → drafted"
            " → reviewed → final → published 推进）"
        )
    o.status = dst  # type: ignore[assignment]
    store.save_outline(o)
    return o
