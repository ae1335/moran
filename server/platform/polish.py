"""反 AI 味后处理管线（§9.2，发布前强制）。

1. 规则层（零成本，本地）：AI 高频词替换 + 句长方差检测
2. 模型层（廉价模型）：按 style-fingerprint 重写标记段落，产出 diff 供确认
3. 复读检测：跨章 n-gram 指纹比对
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..core.store import NovelStore, count_cn_words
from ..model.gateway import ModelGateway


# 规则层替换表（保持语义不变，只去套语）
REPLACE_RULES: list[tuple[str, str]] = [
    ("仿佛整个世界", "四周"),
    ("仿佛", "像"),
    ("宛如", "如同"),
    ("不由得", ""),
    ("不由自主", ""),
    ("眼中闪过一丝", "眼神一沉，"),
    ("嘴角勾起一抹", "笑了笑，"),
    ("心中暗道", "心想"),
    ("心中一凛", "心里一紧"),
    ("不禁", ""),
    ("似乎", "好像"),
    ("在这一刻", "此时"),
    ("与此同时", "同时"),
    ("毋庸置疑", "显然"),
    ("总而言之", "总之"),
    ("值得一提的是", "另外"),
]

# 排比三连检测：「A，B，C」三项结构且每项 ≥4 字
PARALLEL_RE = re.compile(r"([^，。！？\n]{4,12})，([^，。！？\n]{4,12})，([^，。！？\n]{4,12})，")


@dataclass
class PolishIssue:
    kind: str
    original: str
    suggestion: str
    count: int = 1
    line: int = 0


@dataclass
class PolishResult:
    ch: int
    original_words: int
    issues: list[PolishIssue] = field(default_factory=list)
    rewritten: str = ""
    diff: str = ""
    applied: bool = False
    warnings: list[str] = field(default_factory=list)


class AIPolish:
    def __init__(self, store: NovelStore, gateway: ModelGateway | None = None):
        self.store = store
        self.gw = gateway

    # ---------------- 规则层 ----------------
    def rule_layer(self, text: str) -> tuple[str, list[PolishIssue]]:
        issues: list[PolishIssue] = []
        out = text
        for bad, good in REPLACE_RULES:
            n = out.count(bad)
            if n == 0:
                continue
            issues.append(
                PolishIssue(kind="替换", original=bad, suggestion=good or "（删除）", count=n)
            )
            out = out.replace(bad, good)
        # 排比三连
        for m in PARALLEL_RE.finditer(out):
            issues.append(
                PolishIssue(
                    kind="排比",
                    original=m.group(0)[:60],
                    suggestion="拆成两句，或删掉其中一项",
                    count=1,
                )
            )
        return out, issues

    def variance_check(self, text: str) -> list[PolishIssue]:
        """句长方差检测（连续等长句报警）。"""
        sentences = [s for s in re.split(r"(?<=[。！？])", text) if len(s.strip()) >= 4]
        issues: list[PolishIssue] = []
        run = 1
        for i in range(1, len(sentences)):
            if abs(len(sentences[i]) - len(sentences[i - 1])) <= 2:
                run += 1
                if run == 5:
                    issues.append(
                        PolishIssue(
                            kind="节奏",
                            original="".join(sentences[i - 4 : i + 1])[:100],
                            suggestion="连续 5 句长度近似，插入一个短句打断节奏",
                        )
                    )
                    run = 1
            else:
                run = 1
        return issues

    def scan(self, text: str, ch: int) -> list[PolishIssue]:
        """只读扫描：检出套语 / 排比 / 节奏 / 跨章复读，不改写、不落盘。

        §9.2 承诺「发布前强制跑反 AI 味管线」，但 Exporter 到缺陷 #123 为止只查
        `book.ai_flavor_pipeline` 这个开关位（真值 True），从不真的扫一遍 —— 实测
        正文里带着 REPLACE_RULES 的套语照样导出、blocked=False。这条接口让导出预检
        能在不碰正文的前提下把套语算进未处理项（改写仍然要作者点「润色」确认）。
        """
        out, issues = self.rule_layer(text)
        issues += self.variance_check(out)
        issues += self.repetition_check(out, ch)
        return issues

    # ---------------- 复读检测 ----------------
    def repetition_check(self, text: str, ch: int, window: int = 3) -> list[PolishIssue]:
        """跨章 n-gram 指纹比对（§9.2 第 3 项）。"""
        issues: list[PolishIssue] = []
        grams = _ngrams(text, 10)
        if not grams:
            return issues
        for prev in range(max(1, ch - window), ch):
            prev_text = self.store.read_chapter(prev)
            if not prev_text.strip():
                continue
            dup = grams & _ngrams(prev_text, 10)
            dup = {g for g in dup if len(set(g)) >= 5}
            if len(dup) >= 8:
                issues.append(
                    PolishIssue(
                        kind="复读",
                        original=next(iter(dup)),
                        suggestion=f"与第 {prev} 章有 {len(dup)} 处 10-gram 重复，需改写",
                        count=len(dup),
                        line=prev,
                    )
                )
        return issues

    # ---------------- 主流程 ----------------
    async def polish(
        self,
        ch: int,
        *,
        use_model: bool = False,
        apply: bool = False,
    ) -> PolishResult:
        text = self.store.read_chapter(ch)
        if not text.strip():
            raise ValueError(f"第 {ch} 章正文为空")
        # 缺陷 #75：已发布章的正文不许直接改写。预览（apply=False）无害，照常出报告；
        # 一旦要落盘就走 Writer 那句同样的硬保护——否则润色会在作者不知情的情况下
        # 改掉读者已经看到的章节，而章纲 status 还停在 published（口径对不上）。
        if apply:
            _o = self.store.load_outline(ch)
            if _o is not None and _o.status == "published":
                raise ValueError(
                    f"第 {ch} 章已发布；如需润色请先撤销发布标记（已发布章正文不允许直接改写）"
                )
        original_words = count_cn_words(text)
        warnings: list[str] = []

        rewritten, issues = self.rule_layer(text)
        issues += self.variance_check(rewritten)
        issues += self.repetition_check(rewritten, ch)

        if use_model and self.gw is not None and issues:
            try:
                style = self.store.read_truth_text("style-fingerprint.md")
                marked = rewritten
                for iss in issues[:8]:
                    if iss.original and iss.original in marked:
                        marked = marked.replace(iss.original, f"<<{iss.original}>>", 1)
                prompt = (
                    f"# 文风指纹\n{style[:1500]}\n\n"
                    "把下面正文里被 <<>> 标记的片段按文风指纹重写，保持字数与情节不变，"
                    "只输出改写后的完整正文，不要解释：\n\n" + marked[:15000]
                )
                new_text = await self.gw.complete_text("polish", prompt, chapter=ch)
                new_text = re.sub(r"<<|>>", "", new_text).strip()
                if new_text and abs(count_cn_words(new_text) - original_words) < original_words * 0.3:
                    rewritten = new_text
                else:
                    warnings.append("模型改写字数偏离过大，已保留规则层结果")
            except Exception as exc:
                warnings.append(f"模型层改写失败（{type(exc).__name__}），已保留规则层结果")

        diff = _unified(text, rewritten, f"chapters/ch-{ch:04d}.md")
        result = PolishResult(
            ch=ch,
            original_words=original_words,
            issues=issues,
            rewritten=rewritten,
            diff=diff,
            warnings=warnings,
        )
        if apply and rewritten.strip() != text.strip():
            # 留旧版
            v = self.store.next_version_number(ch)
            self.store.write_version(ch, v, text, {"model": "pre-polish", "note": "反AI味改写前留档"})
            self.store.write_chapter(ch, rewritten)
            result.applied = True
        return result


def _ngrams(text: str, n: int) -> set[str]:
    cleaned = "".join(ch for ch in text if not ch.isspace() and ch not in "#*")
    return {cleaned[i : i + n] for i in range(0, max(0, len(cleaned) - n + 1))}


def _unified(old: str, new: str, name: str) -> str:
    import difflib

    return "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True), new.splitlines(keepends=True), fromfile=f"a/{name}", tofile=f"b/{name}"
        )
    )
