"""拆书 Agent（§6.5 / §10）：对标书 TXT → 结构/节奏/钩子三产物（离线批处理）。

版权红线（§10.1）：产物只保留抽象结构数据，不存原文片段入模板。
"""
from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core import atomic
from ..core.store import NovelStore
from ..core.templates import parse_template
from ..model.gateway import ModelGateway
from .prompts import DECONSTRUCTOR_SYSTEM, deconstructor_prompt

CHAPTER_HEAD_RE = re.compile(
    r"^\s*(?:第\s*[0-9一二三四五六七八九十百千万零〇两]+\s*[章回节]|Chapter\s+\d+|CHAPTER\s+\d+)\s*[^\n]{0,60}$",
    re.MULTILINE,
)


@dataclass
class ChapterSlice:
    index: int
    title: str
    text: str
    words: int


@dataclass
class BenchmarkResult:
    id: str
    title: str
    chapters: int
    words: int
    products: list[str] = field(default_factory=list)
    structure: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def split_chapters(raw: str) -> list[ChapterSlice]:
    """按「第N章」切分。切不出来时按固定字数硬切（长文本兜底）。"""
    raw = raw.replace("\r\n", "\n").replace("\r", "\n")
    marks = list(CHAPTER_HEAD_RE.finditer(raw))
    out: list[ChapterSlice] = []
    if len(marks) >= 3:
        for i, m in enumerate(marks):
            start = m.start()
            end = marks[i + 1].start() if i + 1 < len(marks) else len(raw)
            body = raw[start:end].strip()
            title = m.group(0).strip()
            out.append(ChapterSlice(index=i + 1, title=title, text=body, words=count_words(body)))
        return out
    # 兜底：按 3000 字硬切
    size = 3000
    for i in range(0, len(raw), size):
        body = raw[i : i + size]
        if body.strip():
            out.append(ChapterSlice(index=len(out) + 1, title=f"片段 {len(out) + 1}", text=body, words=count_words(body)))
    return out


def count_words(text: str) -> int:
    from ..core.store import count_cn_words

    return count_cn_words(text)


# ---------------------------------------------------------------- 启发式分析（无需模型也能出产物）

HOOK_PATTERNS = {
    "危机型": ["突然", "猛地", "陡然", "危机", "危险", "杀意", "血色", "逼近"],
    "悬念型": ["？", "究竟", "到底", "不知", "为何", "秘密", "竟然"],
    "反转型": ["却是", "没想到", "原来", "反而", "竟然不是", "真相"],
    "情绪型": ["……", "心中一", "泪", "咬牙", "握紧", "沉默", "许久"],
}


def heuristic_analysis(chapters: list[ChapterSlice]) -> dict[str, Any]:
    """纯算法拆解：节奏、钩子配比、对话/动作密度。合规安全（不输出原文）。"""
    if not chapters:
        return {}
    words = [c.words for c in chapters]
    dialogue_ratios: list[float] = []
    action_ratios: list[float] = []
    hook_counts = {k: 0 for k in HOOK_PATTERNS}
    for c in chapters:
        t = c.text
        n = max(1, len(t))
        quotes = sum(t.count(q) for q in ("“", "「", "\""))
        dialogue_ratios.append(quotes / n * 100)
        verbs = sum(t.count(v) for v in ("剑", "拳", "冲", "退", "斩", "踏", "撞", "抓"))
        action_ratios.append(verbs / n * 100)
        tail = t[-300:]
        for kind, pats in HOOK_PATTERNS.items():
            if any(p in tail for p in pats):
                hook_counts[kind] += 1

    total_hooks = sum(hook_counts.values()) or 1
    hook_ratio = {k: round(v / total_hooks, 3) for k, v in hook_counts.items()}

    # 爽点曲线：按每 10 章一个窗口取「动作密度」做代理指标
    window = 10
    curve: list[float] = []
    for i in range(0, len(chapters), window):
        seg = action_ratios[i : i + window]
        curve.append(round(sum(seg) / max(1, len(seg)), 3))

    return {
        "avg_words": int(sum(words) / len(words)) if words else 0,
        "min_words": min(words) if words else 0,
        "max_words": max(words) if words else 0,
        "dialogue_ratio": round(sum(dialogue_ratios) / len(dialogue_ratios) / 100, 3) if dialogue_ratios else 0,
        "action_ratio": round(sum(action_ratios) / len(action_ratios) / 100, 3) if action_ratios else 0,
        "hook_ratio": hook_ratio,
        "coolpoint_curve": curve,
        "chapters": len(chapters),
    }


class Deconstructor:
    def __init__(self, store: NovelStore, gateway: ModelGateway | None = None):
        self.store = store
        self.gw = gateway

    async def analyze(
        self,
        *,
        title: str,
        content: str | None = None,
        source_path: Path | None = None,
        use_model: bool = True,
    ) -> BenchmarkResult:
        if content is None and source_path is not None:
            content = Path(source_path).read_text(encoding="utf-8", errors="replace")
        if not content:
            raise ValueError("拆书输入为空：请提供 content 或 source_path")
        chapters = split_chapters(content)
        if not chapters:
            raise ValueError("无法切分章节：文本为空")
        total_words = sum(c.words for c in chapters)
        warnings: list[str] = []

        heur = heuristic_analysis(chapters)
        structure = {
            "volumes": self._guess_volumes(chapters),
            "coolpoint_curve": heur["coolpoint_curve"],
            "avg_words": heur["avg_words"],
            "min_words": heur["min_words"],
            "max_words": heur["max_words"],
            "opening_pattern": "（启发式：前 3 章是否设钩）" + ("是" if self._has_hook(chapters[0]) else "否"),
            "golden_finger": "",
            "power_up_interval_ch": self._guess_power_interval(chapters),
        }
        hooks = {
            "types": heur["hook_ratio"],
            "sample_notes": "仅统计模式占比，不含原文",
        }
        pacing = {
            "avg_words": heur["avg_words"],
            "dialogue_ratio": heur["dialogue_ratio"],
            "action_ratio": heur["action_ratio"],
            "info_release": "（启发式）每章结尾出现新信息的比例见 hooks",
            "conflict_interval_ch": self._guess_conflict_interval(chapters),
        }
        template: dict[str, Any] = {
            "genre": self.store.load_book().genre,
            "arc_length_ch": structure["volumes"][0]["chapters"] if structure["volumes"] else len(chapters),
            "opening_hook_type": max(heur["hook_ratio"], key=heur["hook_ratio"].get) if heur["hook_ratio"] else "",
            "notes": "由拆书 Agent 自动提炼，仅供节奏对标",
        }

        model_note = "heuristic"
        if use_model and self.gw is not None:
            try:
                payload = await self.gw.complete_json(
                    "deconstruct",
                    deconstructor_prompt(title=title, chapter_texts=[c.text for c in chapters]),
                    system=DECONSTRUCTOR_SYSTEM,
                )
                # 统计口径的字段（字数/密度/曲线）**只信启发式**：模型给出的数字是
                # 估算甚至编造，覆盖实测值会让报告自相矛盾（实测：structure 写 366、
                # pacing.md 写 2200）。模型只负责定性字段（金手指、卷名、备注等）。
                RAW_STAT_KEYS = {
                    "avg_words", "min_words", "max_words", "words", "chapters",
                    "dialogue_ratio", "action_ratio", "coolpoint_curve", "hook_ratio",
                    # volumes 是按实际章数推算的，模型常报「60 章一卷」这类幻觉数字
                    "volumes", "power_up_interval_ch",
                }
                m_struct = {k: v for k, v in (payload.get("structure") or {}).items() if k not in RAW_STAT_KEYS}
                m_pacing = {k: v for k, v in (payload.get("pacing") or {}).items() if k not in RAW_STAT_KEYS}
                structure = _merge_dict(structure, m_struct)
                pacing = _merge_dict(pacing, m_pacing)
                hooks = _merge_dict(hooks, payload.get("hooks") or {})
                template = _merge_dict(template, payload.get("template") or {})
                model_note = "heuristic+model"
            except Exception as exc:
                warnings.append(f"模型拆书失败（{type(exc).__name__}），仅启发式产物")

        # 落盘：四个产物分批写，中途失败必须不留残缺目录（缺陷 #93）
        bid = _safe_dir(title)
        base = self.store.p.root / "analysis" / "benchmarks" / bid
        # 目录是本次新建的 → 失败就整目录删掉；覆盖已有产物时保留旧文件
        # （旧的至少是完整的，比「一半新一半旧」好）。
        fresh = not base.exists()
        base.mkdir(parents=True, exist_ok=True)
        try:
            self._write_products(
                base,
                title,
                structure,
                pacing,
                hooks,
                template,
                chapters,
                total_words,
                model_note,
            )
        except BaseException:
            if fresh:
                shutil.rmtree(base, ignore_errors=True)
            raise

        # 同步到模板库（§10.2：新书初始化可用）
        # 书内镜像 + 工作区共享库（M8「共享模板」：拆一本对标书，所有书都能用）
        tpl_payload = {**template, "source": bid, "id": bid}
        tpl_dir = self.store.p.root / "analysis" / "templates"
        tpl_dir.mkdir(parents=True, exist_ok=True)
        atomic.write_json_atomic(tpl_dir / f"{bid}.json", tpl_payload)
        try:
            self.store.workbench_template_library().save(parse_template(tpl_payload, id=bid))
        except Exception as exc:  # 共享库写失败不影响拆书本身
            warnings.append(f"模板同步共享库失败（{type(exc).__name__}），仅保留书内镜像")

        return BenchmarkResult(
            id=bid,
            title=title,
            chapters=len(chapters),
            words=total_words,
            products=["structure.json", "pacing.md", "hooks.md", "template.json"],
            structure=structure,
            warnings=warnings,
        )

    def _write_products(
        self,
        base: Path,
        title: str,
        structure: dict[str, Any],
        pacing: dict[str, Any],
        hooks: dict[str, Any],
        template: dict[str, Any],
        chapters: list[ChapterSlice],
        total_words: int,
        model_note: str,
    ) -> None:
        """把四个产物写到 `base`（缺陷 #93 拆分出来，便于失败时整目录回滚）。"""
        atomic.write_json_atomic(
            base / "structure.json",
            {**structure, "title": title, "chapters": len(chapters), "words": total_words, "model": model_note},
        )
        atomic.write_text_atomic(
            base / "pacing.md",
            f"""# 《{title}》节奏分析

- 章节数：{len(chapters)}
- 总字数：{total_words:,}
- 平均章长：{pacing['avg_words']} 字（{structure['min_words']}–{structure['max_words']}）
- 对话密度：{pacing['dialogue_ratio']:.0%}
- 动作密度：{pacing['action_ratio']:.0%}
- 冲突间隔：约 {pacing['conflict_interval_ch']} 章一次
- 信息释放：{pacing['info_release']}

## 爽点密度曲线（每 10 章窗口的动作密度代理）
{_sparkline(structure['coolpoint_curve'])}
""",
        )
        atomic.write_text_atomic(
            base / "hooks.md",
            f"""# 《{title}》断章钩子模式

| 类型 | 占比 |
|---|---|
""" + "\n".join(f"| {k} | {v:.0%} |" for k, v in sorted(hooks["types"].items(), key=lambda x: -x[1]))
            + f"""

- 主导钩型：{template.get('opening_hook_type') or '（未识别）'}
- 样本说明：{hooks.get('sample_notes', '')}
""",
        )
        atomic.write_json_atomic(base / "template.json", template)

    # ---------------- 启发式小工具 ----------------
    @staticmethod
    def _guess_volumes(chapters: list[ChapterSlice]) -> list[dict[str, Any]]:
        n = len(chapters)
        if n == 0:
            return []
        if n < 60:
            return [{"index": 1, "chapters": n, "note": "短篇/单卷"}]
        per = 60
        return [
            {"index": i // per + 1, "chapters": min(per, n - i), "note": f"第 {i + 1}–{min(n, i + per)} 章"}
            for i in range(0, n, per)
        ]

    @staticmethod
    def _has_hook(c: ChapterSlice) -> bool:
        tail = c.text[-300:]
        return any(p in tail for pats in HOOK_PATTERNS.values() for p in pats[:2])

    @staticmethod
    def _guess_power_interval(chapters: list[ChapterSlice]) -> int:
        """粗略：出现「突破/晋级/进阶」的章节间隔。"""
        hits = [i for i, c in enumerate(chapters) if any(k in c.text for k in ("突破", "晋级", "进阶", "晋升"))]
        if len(hits) < 2:
            return 10
        gaps = [hits[i + 1] - hits[i] for i in range(len(hits) - 1)]
        return max(1, int(sum(gaps) / len(gaps)))

    @staticmethod
    def _guess_conflict_interval(chapters: list[ChapterSlice]) -> int:
        hits = [i for i, c in enumerate(chapters) if any(k in c.text for k in ("冲突", "对峙", "出手", "打了起来", "战"))]
        if len(hits) < 2:
            return 3
        gaps = [hits[i + 1] - hits[i] for i in range(len(hits) - 1)]
        return max(1, int(sum(gaps) / len(gaps)))


def list_benchmarks(root: Path) -> list[dict[str, Any]]:
    base = Path(root) / "analysis" / "benchmarks"
    out: list[dict[str, Any]] = []
    if not base.exists():
        return out
    for d in sorted(base.iterdir()):
        if not d.is_dir():
            continue
        s = atomic.read_json(d / "structure.json", {}) or {}
        # 缺陷 #98：产物被外部写成标量/列表时 s.get 抛 AttributeError，
        # 实测 GET /benchmarks 整条 500。产物读不出就当没有 meta（目录仍列出，
        # 作者能看到这本书需要重拆）。
        if not isinstance(s, dict):
            s = {}
        out.append(
            {
                "id": d.name,
                "title": s.get("title", d.name),
                "chapters": s.get("chapters", 0),
                "words": s.get("words", 0),
                "products": sorted(p.name for p in d.iterdir() if p.is_file()),
            }
        )
    return out


def _merge_dict(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in (patch or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = {**out[k], **v}
        elif v not in (None, "", [], {}):
            out[k] = v
    return out


def _sparkline(values: list[float]) -> str:
    if not values:
        return "（无数据）"
    chars = "▁▂▃▄▅▆▇█"
    hi = max(values) or 1
    return "".join(chars[min(7, int(v / hi * 7))] for v in values) + f"  (max {hi:.3f})"


# 缺陷 #82：对标书名长度上界。bid 会直接当目录名落进 analysis/benchmarks/，
# ext4 单名 255 字节、中文 3 字节/字，60 字是安全线。超长书名此前会在 mkdir
# 那一步炸 OSError(Errno 36)，HTTP 层变成一句英文 500（实测 60/200/400 字全是 500）。
MAX_BENCHMARK_TITLE_CHARS = 60


def _safe_dir(name: str) -> str:
    """对标书名 -> 目录名（bid）。

    缺陷 #82：只做字符净化不做长度校验不够——净化后的名字照样能超出文件系统的
    单名上限，而且是在 mkdir 那一刻才炸，作者看到的是看不懂的英文 500。
    超限直接 ValueError（`analyze` 的调用方 routes 会转成 400），不静默截断：
    截断会让两本不同的对标书撞到同一个 bid 上，产物互相覆盖。
    """
    s = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name.strip()).strip(". ")
    if not s:
        return "benchmark"
    if len(s) > MAX_BENCHMARK_TITLE_CHARS:
        raise ValueError(f"对标书名过长：{len(s)} 字（上限 {MAX_BENCHMARK_TITLE_CHARS} 字）")
    return s
