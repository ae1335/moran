"""Reviewer（§6.3）：双视角 + 平台 profile → 结构化问题清单。

「双视角」= 责编视角（平台合规/商业性）+ 读者视角（代入感/爽点/断章钩），
两类问题合并在同一 issues 里，用 category 区分。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..core.schema import ChapterOutline, ReviewIssue, ReviewReport
from ..core.store import NovelStore, count_cn_words
from ..model.gateway import ModelGateway
from .prompts import REVIEWER_SYSTEM, parse_issues, reviewer_prompt

VALID_CATEGORIES = {"OOC", "power", "timeline", "pov", "repetition", "ai-flavor", "hook", "redline", "words"}

# 规则层（零成本，本地）：§9.2 第 1 层
AI_SMELL_WORDS = [
    "仿佛", "宛如", "不由得", "不禁", "似乎", "彷佛",
    "眼中闪过一丝", "嘴角勾起一抹", "心中暗道", "心中一凛", "不由自主",
    "在这一刻", "与此同时", "值得一提的是", "毋庸置疑", "总而言之",
    "宛如一幅画", "如同一首", "仿佛整个世界",
]
PARALLELISM_RE = None  # 运行时编译


@dataclass
class ReviewResult:
    report: ReviewReport
    path: str = ""
    counts: dict[str, int] = field(default_factory=dict)


class Reviewer:
    def __init__(self, store: NovelStore, gateway: ModelGateway):
        self.store = store
        self.gw = gateway

    async def review(
        self,
        ch: int,
        *,
        scope: str = "full",
        use_model: bool = True,
        save: bool = True,
    ) -> ReviewResult:
        text = self.store.read_chapter(ch)
        if not text.strip():
            raise ValueError(f"第 {ch} 章正文为空，无法审稿")
        outline = self.store.load_outline(ch) or ChapterOutline(ch=ch)
        book = self.store.load_book()
        profile = self.store.load_profile(book.platform)
        words = count_cn_words(text)

        # ---- 规则层（本地，零成本）----
        issues: list[ReviewIssue] = []
        issues += self._rule_words(words, profile, outline)
        issues += self._rule_redline(text, profile)
        issues += self._rule_hook(text, outline, profile)
        issues += self._rule_ai_flavor(text)
        issues += self._rule_repetition(text, ch)
        issues += self._rule_pov(text, outline)
        issues += self._rule_power(text, outline)

        # ---- 模型层（可选）----
        model_note = "rules"
        if use_model:
            chars = []
            for cid in outline.char_refs():
                c = self.store.get_character(cid)
                if c:
                    chars.append(c)
            if not chars:
                chars = self.store.list_characters()[:6]
            timeline = self._timeline_brief()
            style = self.store.read_truth_text("style-fingerprint.md")
            recent = "\n".join(
                f"第{p}章：{self.store.read_summary(p)[:200]}" for p in range(max(1, ch - 3), ch)
                if self.store.read_summary(p).strip()
            )
            prompt = reviewer_prompt(
                ch=ch,
                text=text[:20000],
                outline=outline,
                profile=profile,
                characters=chars,
                timeline=timeline,
                style=style,
                recent_summaries=recent,
                word_count=words,
            )
            try:
                payload = await self.gw.complete_json("review", prompt, system=REVIEWER_SYSTEM, chapter=ch)
                model_issues = parse_issues(payload, allowed=VALID_CATEGORIES)
                # §6.3 硬要求：evidence 必须是正文里真实存在的原文片段。
                # 不校验的话，模型会把 prompt 里「禁用词清单」当成「正文命中」——
                # 文风指纹写着「忌：眼中闪过一丝」，它就报这条命中（已实测复现）。
                model_issues, rejected = _verify_evidence(model_issues, text)
                issues = _merge_issues(issues, model_issues)
                model_note = "rules+model"
                if rejected:
                    model_note += f" (丢弃 {len(rejected)} 条无证据问题)"
            except Exception as exc:  # 模型审稿失败不能阻断：规则层结果照常返回
                model_note = f"rules (model failed: {type(exc).__name__})"

        issues.sort(key=lambda i: {"high": 0, "mid": 1, "low": 2}.get(i.severity, 3))
        counts: dict[str, int] = {}
        for i in issues:
            counts[i.severity] = counts.get(i.severity, 0) + 1
            counts[f"cat:{i.category}"] = counts.get(f"cat:{i.category}", 0) + 1

        report = ReviewReport(
            ch=ch,
            issues=issues,
            model=model_note,
            created=_now(),
            scope=scope,
            stats={"words": words, "counts": counts, "categories": sorted({i.category for i in issues})},
        )
        path = ""
        if save:
            self.store.save_review(report)
            path = self.store.rel(self.store.p.review_json(ch))
            if outline.status in ("drafted", "writing"):
                outline.status = "reviewed"  # type: ignore[assignment]
                self.store.save_outline(outline)
        return ReviewResult(report=report, path=path, counts=counts)

    # ---------------- 规则检查 ----------------
    def _rule_words(self, words: int, profile, outline: ChapterOutline) -> list[ReviewIssue]:
        lo, hi = profile.chapter_words
        out: list[ReviewIssue] = []
        if words < lo:
            out.append(
                ReviewIssue(
                    severity="high" if words < lo * 0.7 else "mid",
                    category="words",
                    evidence=f"实际 {words} 字",
                    suggestion=f"平台 {profile.platform} 要求 {lo}–{hi} 字，需补写约 {lo - words} 字",
                )
            )
        elif words > hi:
            out.append(
                ReviewIssue(
                    severity="mid",
                    category="words",
                    evidence=f"实际 {words} 字",
                    suggestion=f"超出 {profile.platform} 的 {lo}–{hi} 字上限，建议拆章",
                )
            )
        return out

    def _rule_redline(self, text: str, profile) -> list[ReviewIssue]:
        out: list[ReviewIssue] = []
        for w in profile.redline_words:
            if not w:
                continue
            idx = text.find(w)
            if idx >= 0:
                out.append(
                    ReviewIssue(
                        severity="high",
                        category="redline",
                        evidence=text[max(0, idx - 20) : idx + len(w) + 20],
                        suggestion=f"红线词「{w}」需替换或删除",
                    )
                )
        return out

    def _rule_hook(self, text: str, outline: ChapterOutline, profile) -> list[ReviewIssue]:
        if not profile.hook_required:
            return []
        tail = text[-400:] if profile.hook_position == "last_200_words" else text[-300:]
        # 钩子判定：结尾是否含未解信息/悬念标记
        markers = ["？", "！", "突然", "却", "竟然", "忽然", "陡然", "没想到", "不知", "谁知", "……"]
        has_marker = any(m in tail for m in markers)
        ends_flat = tail.rstrip().endswith(("了。", "吧。", "呢。", "。"))
        if not has_marker or (ends_flat and len(tail.rstrip()) < 40):
            return [
                ReviewIssue(
                    severity="mid",
                    category="hook",
                    evidence=tail[-80:] or "（章节过短）",
                    suggestion="结尾钩子偏弱：补一句未解信息或制造悬念的短句（危机/悬念/反转/情绪任选其一）",
                )
            ]
        return []

    def _rule_ai_flavor(self, text: str) -> list[ReviewIssue]:
        out: list[ReviewIssue] = []
        for w in AI_SMELL_WORDS:
            n = text.count(w)
            if n == 0:
                continue
            limit = 1 if len(w) >= 3 else 3
            if n > limit:
                idx = text.find(w)
                out.append(
                    ReviewIssue(
                        severity="high" if n > limit * 3 else "mid",
                        category="ai-flavor",
                        evidence=text[max(0, idx - 15) : idx + len(w) + 15],
                        suggestion=f"「{w}」出现 {n} 次（建议 ≤{limit}）：改成具体动作/感官描写",
                    )
                )
        # 句长方差：连续 5 句长度接近 → 机器感
        sentences = [s for s in _split_sentences(text) if len(s.strip()) > 4]
        if len(sentences) >= 8:
            lengths = [len(s) for s in sentences]
            run = 1
            for i in range(1, len(lengths)):
                if abs(lengths[i] - lengths[i - 1]) <= 2:
                    run += 1
                    if run >= 5:
                        out.append(
                            ReviewIssue(
                                severity="low",
                                category="ai-flavor",
                                evidence="；".join(sentences[i - 4 : i + 1])[:120],
                                suggestion="连续 5 句长度几乎相同，打破节奏：插入一个短句或长句",
                            )
                        )
                        break
                else:
                    run = 1
        return out

    def _rule_repetition(self, text: str, ch: int) -> list[ReviewIssue]:
        out: list[ReviewIssue] = []
        if ch <= 1:
            return out
        prev = self.store.read_chapter(ch - 1)
        if not prev.strip():
            return out
        # 跨章复读：只看**连续长片段**（≥30 字），不看 8-gram 集合大小
        runs = [r for r in _shared_runs(prev, text, min_len=30) if len(set(r)) > 4]
        longest = max((len(r) for r in runs), default=0)
        if longest >= 60 or len(runs) >= 3:
            worst = max(runs, key=len) if runs else ""
            out.append(
                ReviewIssue(
                    severity="high" if longest >= 120 else "mid",
                    category="repetition",
                    evidence=worst[:40],
                    suggestion=(
                        f"与上一章有 {len(runs)} 段连续重复（最长 {longest} 字），"
                        f"疑似复读桥段，建议改写或删减"
                    ),
                )
            )
        # 章内自我复读：按**整句**统计，比 8-gram 更贴近作者感知
        sentences = [x.strip() for x in re.split(r"[。！？\n]+", text) if len(x.strip()) >= 12]
        counts: dict[str, int] = {}
        for sent in sentences:
            counts[sent] = counts.get(sent, 0) + 1
        heavy = sorted(((k, v) for k, v in counts.items() if v >= 3), key=lambda kv: -kv[1])
        if heavy:
            sent, times = heavy[0]
            out.append(
                ReviewIssue(
                    severity="mid",
                    category="repetition",
                    evidence=sent[:40],
                    suggestion=f"同一句话在本章出现 {times} 次，做同义替换或删减",
                )
            )
        return out

    def _rule_pov(self, text: str, outline: ChapterOutline) -> list[ReviewIssue]:
        """粗粒度 POV 漂移：出现「他不知道的是」「殊不知」「却不知」= 全知视角入侵。"""
        out: list[ReviewIssue] = []
        for marker in ("他不知道的是", "殊不知", "却不知", "而此刻的", "与此同时，在"):
            idx = text.find(marker)
            if idx >= 0:
                out.append(
                    ReviewIssue(
                        severity="mid",
                        category="pov",
                        evidence=text[max(0, idx - 20) : idx + 40],
                        suggestion=f"「{marker}」越出 {outline.pov} 的限制视角，改为该角色可感知的线索",
                    )
                )
        return out

    def _rule_power(self, text: str, outline: ChapterOutline) -> list[ReviewIssue]:
        """战力崩坏：动用 forbidden 之外的力量词。"""
        out: list[ReviewIssue] = []
        chars = {c.id: c for c in self.store.list_characters()}
        pov = outline.pov.lstrip("@")
        if pov.startswith("characters/"):
            pov = pov[len("characters/") :]
        card = chars.get(pov)
        if card is None or not card.power_scale.forbidden:
            return out
        forbidden = card.power_scale.forbidden
        # 从「不得突破凝气期」这类描述里抠出境界词
        import re

        realm = re.search(r"不得突破(.{2,6}?)期", forbidden)
        if realm:
            word = realm.group(1) + "期"
            if word in text:
                idx = text.find(word)
                out.append(
                    ReviewIssue(
                        severity="high",
                        category="power",
                        evidence=text[max(0, idx - 25) : idx + 30],
                        suggestion=f"违反人物卡约束「{forbidden}」：出现「{word}」",
                    )
                )
        return out

    def _timeline_brief(self) -> str:
        entries = self.store.load_timeline()[-12:]
        return "\n".join(f"- 第{e.ch}章 {e.date}：{e.event}" for e in entries)


# ---------------------------------------------------------------- 工具


def _split_sentences(text: str) -> list[str]:
    import re

    return [s for s in re.split(r"(?<=[。！？；])", text) if s]


def _ngrams(text: str, n: int) -> set[str]:
    cleaned = "".join(ch for ch in text if not ch.isspace())
    return {cleaned[i : i + n] for i in range(0, max(0, len(cleaned) - n + 1))}


def _shared_runs(a: str, b: str, min_len: int = 30) -> list[str]:
    """两段文本之间「连续共同片段」列表（长度 ≥ ``min_len``，已合并相邻）。

    为什么不用 8-gram 集合大小：那会把「三十个分散的常见 8-gram」和
    「整段 500 字照搬」算成同一件事。实测 mock 样本报出「774 处重复」，
    实际只是模板池复用造成的短串碰撞——这种数字对作者没有指导意义。
    真正的复读是**连续长片段**，所以这里按连续共同子串统计。
    """
    clean_a = "".join(ch for ch in a if not ch.isspace())
    clean_b = "".join(ch for ch in b if not ch.isspace())
    if not clean_a or not clean_b:
        return []

    n = len(clean_a)
    if n < min_len:
        return []
    # 缺陷 #97：原先每个窗口都做一次 `in clean_b`（O(n) 子串查找）+ 逐字扩展，
    # 整体 O(n^2)。实测两章各 5 万字要 1.6s、10 万字（MAX_CHAPTER_CHARS 上限）要
    # **6.6s**——审稿是每次保存都要跑的，作者直接卡住。
    # 改成：先把 b 的所有 min_len 窗口做成集合（O(n) 建、O(1) 查），命中后再用二分
    # 求最长延伸（O(log n)）。300 组随机对照 fuzz 与旧实现输出逐条一致。
    grams = {clean_b[k : k + min_len] for k in range(0, len(clean_b) - min_len + 1)}
    runs: list[str] = []
    i = 0
    while i + min_len <= n:
        if clean_a[i : i + min_len] not in grams:
            i += 1
            continue
        lo, hi = min_len, n - i
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if clean_a[i : i + mid] in clean_b:
                lo = mid
            else:
                hi = mid - 1
        runs.append(clean_a[i : i + lo])
        i += lo
    # 只有标点重合（如「。」）不算复读
    return [r for r in runs if len(set(r)) > 4]


#: 这些 category 的 evidence 必须是正文里的字面片段；其余（hook/words/pov）可以是描述
LITERAL_EVIDENCE_CATEGORIES = {"OOC", "power", "repetition", "ai-flavor", "redline", "timeline"}


def _norm_ws(t: str) -> str:
    return "".join(ch for ch in t if not ch.isspace())


def _verify_evidence(
    issues: list[ReviewIssue], text: str
) -> tuple[list[ReviewIssue], list[ReviewIssue]]:
    """证据校验：把 evidence 不在正文里的模型问题剔掉（它们几乎都是幻觉）。

    对 LITERAL_EVIDENCE_CATEGORIES 强制要求 evidence 是正文的字面子串（忽略空白）。
    """
    body = _norm_ws(text)
    kept: list[ReviewIssue] = []
    rejected: list[ReviewIssue] = []
    for issue in issues:
        if issue.category not in LITERAL_EVIDENCE_CATEGORIES:
            kept.append(issue)
            continue
        ev = (issue.evidence or "").strip()
        if not ev:
            rejected.append(issue)
            continue
        # evidence 可能被模型截断/加省略号，取前 8 个有效字符做包含判定
        probe = _norm_ws(ev.replace("…", "").replace("...", ""))[:8]
        if len(probe) < 2 or probe in body:
            kept.append(issue)
        else:
            rejected.append(issue)
    return kept, rejected


def _merge_issues(rule_issues: list[ReviewIssue], model_issues: list[ReviewIssue]) -> list[ReviewIssue]:
    """合并去重：同 category + 同 evidence 前 20 字视为同一条，取更严重的。"""
    seen: dict[tuple[str, str], ReviewIssue] = {}
    rank = {"high": 0, "mid": 1, "low": 2}
    for issue in rule_issues + model_issues:
        key = (issue.category, (issue.evidence or "")[:20])
        cur = seen.get(key)
        if cur is None or rank.get(issue.severity, 3) < rank.get(cur.severity, 3):
            seen[key] = issue
    return list(seen.values())


def _now() -> str:
    import datetime as dt

    return dt.datetime.now().isoformat(timespec="seconds")
