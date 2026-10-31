"""单条审稿问题的改写建议（§13.2「problem list adopt/ignore + diff preview」）。

设计要点：
- **只对能定位的问题产生 diff**：evidence 必须能在正文里找到，否则无从改起；
  这类问题只返回 ``applicable=False`` 和原因，前端显示"需人工处理"。
- **改写来自规则层**：套语替换、复读删减、POV 标记删除、红线遮蔽提示。
  不走模型——作者只是想看"如果采纳会变成什么样"，不是重新生成整章。
- **不改文件**：本模块只产 diff 文本与替换后的完整正文，落盘由作者另行确认
  （走 Writer 的版本机制，或 polish 的 apply）。
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field

from ..core.schema import ReviewIssue
from ..core.store import NovelStore

#: 套语 → 建议替换（与 polish.REPLACE_RULES 保持同一口径，但这里只替换命中的那一处）
SAYING_REPLACEMENTS: dict[str, str] = {
    "仿佛": "像",
    "宛如": "像",
    "犹如": "像",
    "不由得": "",
    "不由自主": "",
    "眼中闪过一丝": "眼里掠过一点",
    "心中一凛": "心里一紧",
    "倒吸一口凉气": "吸了口气",
    "眉头微皱": "皱了皱眉",
    "嘴角勾起一抹": "嘴角牵了一下",
    "不由自主地": "",
    "眼中闪过": "眼里掠过",
    "心中一喜": "心里一动",
    "深吸一口气": "吸了口气",
    "缓缓地": "慢慢",
    "顿时": "当下",
    "瞬间": "一下",
}

#: 结构类问题为什么不能自动改（给作者看的原因文案）
STRUCTURAL_REASONS = {
    "words": "章节字数不在目标区间，需要增删情节或扩写，无法局部替换",
    "hook": "章末钩子偏弱，需要在结尾加一句未解信息，请手动改写末段",
    "title": "章节名长度或风格不符，请改名后重试",
}

#: POV 入侵标记 → 直接删除整句
POV_MARKERS = ("他不知道的是", "殊不知", "却不知", "而此刻的", "与此同时，在")


@dataclass
class Suggestion:
    """一条问题的改写建议。"""

    issue_index: int
    category: str
    severity: str
    applicable: bool
    reason: str = ""
    before: str = ""
    after: str = ""
    diff: str = ""
    note: str = ""


@dataclass
class SuggestionBatch:
    ch: int
    suggestions: list[Suggestion] = field(default_factory=list)
    applied_text: str = ""
    original_text: str = ""

    def as_dict(self) -> dict:
        return {
            "ch": self.ch,
            "suggestions": [
                {
                    "issue_index": s.issue_index,
                    "category": s.category,
                    "severity": s.severity,
                    "applicable": s.applicable,
                    "reason": s.reason,
                    "before": s.before,
                    "after": s.after,
                    "diff": s.diff,
                    "note": s.note,
                }
                for s in self.suggestions
            ],
            "changed": self.original_text != self.applied_text,
        }


class FixSuggester:
    """按审稿问题生成可预览的改写（不落盘）。"""

    def __init__(self, store: NovelStore):
        self.store = store

    def suggest(self, ch: int, issue_indices: list[int] | None = None) -> SuggestionBatch:
        text = self.store.read_chapter(ch)
        report = self.store.load_review(ch)
        if report is None:
            return SuggestionBatch(ch=ch, original_text=text, applied_text=text)
        if not text.strip():
            return SuggestionBatch(ch=ch, original_text=text, applied_text=text)

        working = text
        batch = SuggestionBatch(ch=ch, original_text=text)
        for idx, issue in enumerate(report.issues):
            if issue_indices is not None and idx not in issue_indices:
                continue
            sug = self._one(idx, issue, working)
            batch.suggestions.append(sug)
            if sug.applicable and sug.before and sug.after != sug.before:
                # 只替换第一处，避免同一套语被一次性全改（作者要看单点效果）
                working = working.replace(sug.before, sug.after, 1)
        batch.applied_text = working
        return batch

    # ---------------- 单条 ----------------
    def _one(self, idx: int, issue: ReviewIssue, text: str) -> Suggestion:
        ev = (issue.evidence or "").strip()
        base = Suggestion(
            issue_index=idx,
            category=issue.category,
            severity=issue.severity,
            applicable=False,
        )
        # 结构类问题本来就没有字面 evidence，先判类别再定位，
        # 否则会误报「evidence 在正文里找不到」（对作者毫无信息量）。
        if issue.category in ("words", "hook", "title"):
            base.reason = STRUCTURAL_REASONS.get(
                issue.category, "结构类问题需要增删内容，不做局部替换"
            )
            return base
        if not ev:
            base.reason = "该问题没有给出正文片段，无法定位"
            return base
        # 定位到 evidence 所在的**整段**：作者看 diff 需要上下文，
        # 只给「仿佛」两个字看不出该改哪里（实测这是最常见的不满）。
        span = self._locate_paragraph(text, ev)
        if span is None:
            base.reason = "evidence 在正文里找不到（可能是跨段引用或被截断）"
            return base
        start, end = span
        before = text[start:end]
        base.before = before

        if issue.category == "ai-flavor":
            after = self._replace_saying(before)
        elif issue.category == "pov":
            after = self._drop_pov(before)
        elif issue.category == "repetition":
            after = self._shorten_repeat(before, ev)
        elif issue.category == "redline":
            base.reason = "红线词不能自动替换，请手动改写或删句"
            return base
        elif issue.category in ("power", "OOC", "timeline"):
            base.reason = "情节/设定类问题需要作者判断，不做自动改写"
            return base
        else:
            base.reason = f"未知类别 {issue.category}，不做自动改写"
            return base

        if after == before:
            base.reason = "规则层没有可用的替换（可能是模型判定的主观问题）"
            return base

        base.applicable = True
        base.after = after
        base.diff = _unified(before, after, issue.category)
        return base

    # ---------------- 改写规则 ----------------
    @staticmethod
    def _replace_saying(text: str) -> str:
        out = text
        # 长词优先，避免「眼中闪过」先被替换导致「眼中闪过一丝」匹配不到
        for bad in sorted(SAYING_REPLACEMENTS, key=len, reverse=True):
            if bad in out:
                good = SAYING_REPLACEMENTS[bad]
                if not good:
                    # 纯删除类：连带去掉紧跟的「地」
                    out = out.replace(bad + "地", "", 1).replace(bad, "", 1)
                else:
                    out = out.replace(bad, good, 1)
                break
        return out

    @staticmethod
    def _drop_pov(text: str) -> str:
        """删掉 POV 入侵标记所在的**整句**，保留同段其他句子。"""
        out = text
        for marker in POV_MARKERS:
            if marker not in out:
                continue
            # 标记句的起点：前一个句末标点之后
            pos = out.find(marker)
            sent_start = max(out.rfind("。", 0, pos), out.rfind("！", 0, pos), out.rfind("？", 0, pos))
            sent_start = 0 if sent_start < 0 else sent_start + 1
            # 标记句的终点：标记之后的第一个句末标点
            sent_end = len(out)
            for i in range(pos + len(marker), len(out)):
                if out[i] in "。！？\n":
                    sent_end = i + 1
                    break
            out = (out[:sent_start] + out[sent_end:]).strip()
            break
        return out

    @staticmethod
    def _shorten_repeat(text: str, evidence: str = "") -> str:
        """复读类：给最小可用动作——删掉重复句。

        优先级：
        1) 段内有完全重复的句子 → 删掉靠后的那一次；
        2) 删掉**evidence 指向的那一句**（reviewer 标的就是它）；
        3) 兜底才退化为「删最长句」。

        第 2 条很重要：早先用「删最长句」，结果把「渡口的缆绳绷得很紧」
        这种场景铺垫句删了、留下真正的复读句（实测过）。
        """
        sentences = [s for s in re.split(r"(?<=[。！？])", text) if s.strip()]
        if len(sentences) < 2:
            return text

        # 1) 段内整句重复
        seen: set[str] = set()
        for i, sent in enumerate(sentences):
            key = sent.strip()
            if len(key) < 10:
                continue
            if key in seen:
                return "".join(sentences[:i] + sentences[i + 1 :]).strip()
            seen.add(key)

        # 2) evidence 指向的句子
        probe = re.sub(r"\s+", "", evidence.strip("。！？，、；：…—\n\r\t ·"))
        if probe:
            for i, sent in enumerate(sentences):
                if probe[:12] and probe[:12] in re.sub(r"\s+", "", sent):
                    return "".join(sentences[:i] + sentences[i + 1 :]).strip()

        # 3) 兜底
        longest = max(range(len(sentences)), key=lambda i: len(sentences[i]))
        if len(sentences[longest].strip()) < 12:
            return text
        return "".join(sentences[:longest] + sentences[longest + 1 :]).strip()

    # ---------------- 定位 ----------------
    @staticmethod
    def _locate_paragraph(text: str, evidence: str) -> tuple[int, int] | None:
        """定位 evidence，返回其所在**段落**的 [start, end)。

        Reviewer 给的 evidence 常常带「前一句的句末标点」或跨段空白
        （``。林澈在心里…`` 而正文是 ``\n\n林澈在心里…``），
        直接 find 会失败。这里按「原样 → 去首尾标点 → 去空白」逐级放宽。
        """
        candidates = [evidence]
        stripped = evidence.strip("。！？，、；：…—\n\r\t ·")
        if stripped != evidence:
            candidates.append(stripped)
        # 去掉所有空白再试（跨段引用会带 \n\n）
        flat = re.sub(r"\s+", "", stripped)
        if flat and flat != stripped:
            candidates.append(flat)

        pos = -1
        for cand in candidates:
            if not cand:
                continue
            if cand is candidates[-1] and flat and cand == flat:
                # 扁平化候选：在同样扁平化的正文里定位，再映射回原下标
                flat_text = re.sub(r"\s+", "", text)
                fpos = flat_text.find(cand)
                if fpos < 0:
                    continue
                # 把扁平下标映射回原始下标
                idx_map: list[int] = []
                for i, ch in enumerate(text):
                    if not ch.isspace():
                        idx_map.append(i)
                if fpos >= len(idx_map):
                    continue
                pos = idx_map[fpos]
                break
            pos = text.find(cand)
            if pos >= 0:
                break

        if pos < 0:
            probe = re.sub(r"\s+", "", stripped.replace("…", "").replace("...", ""))
            for n in (12, 10, 8, 6):
                if len(probe) < n:
                    continue
                pos = text.find(probe[:n])
                if pos >= 0:
                    break
        if pos < 0:
            return None
        # 向前找空行或开头
        start = text.rfind("\n\n", 0, pos)
        start = 0 if start < 0 else start + 2
        # 向后找空行或结尾
        end = text.find("\n\n", pos)
        end = len(text) if end < 0 else end
        return start, end


def _unified(before: str, after: str, label: str) -> str:
    """生成统一 diff。

    段落通常只有一行且不带换行符，直接喂给 unified_diff 会把「-旧」和「+新」
    拼成同一行（实测过），作者根本读不出来。这里按行补齐换行符，
    单行段落退化成整段替换，可读性优先。
    """
    a = before.splitlines() or [before]
    b = after.splitlines() or [after]
    # lineterm="\n" 让 header 与内容行都带换行；内容行本身已补 \n，不再重复加
    return "".join(
        difflib.unified_diff(
            [line + "\n" for line in a],
            [line + "\n" for line in b],
            fromfile=f"原文（{label}）",
            tofile=f"建议（{label}）",
            lineterm="\n",
        )
    )
