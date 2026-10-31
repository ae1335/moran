"""读者反馈回流（§12）：录入 + 复盘管线。

弃书率 top-5% 章节 → Reviewer 定向重审 → 归因 → 避雷条目提案 → style-fingerprint。
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from ..core import atomic
from ..core.store import NovelStore


@dataclass
class RecheckResult:
    chapters: list[int]
    findings: list[dict[str, Any]] = field(default_factory=list)
    proposals: list[dict[str, Any]] = field(default_factory=list)
    applied: bool = False


#: 本章避雷的留存上界：超过就丢最旧的（缺陷 #115）。Planner 只读最近 12 条，
#: 留几百条既没用，又让真相源文件全是陈年旧账。
MAX_AVOIDANCE_ENTRIES = 200


class FeedbackIngest:
    def __init__(self, store: NovelStore):
        self.store = store

    # ---------------- 录入 ----------------
    def save_stats(self, payload: dict[str, Any]) -> dict[str, Any]:
        """feedback/stats.json：{days:[{date, reads, retention, chapters:{...}}]}"""
        # 缺陷 #88：读旧 days → 去重覆盖 → 写回，整体必须持锁，
        # 否则并发录入会互相覆盖（实测同类路径 20 线程只剩 3–5 条）。
        with atomic.file_lock(self.store.p.feedback_stats):
            cur = self.store.load_feedback_stats()
            days = cur.get("days") or []
            entry = dict(payload)
            entry.setdefault("date", dt.date.today().isoformat())
            entry["updated"] = dt.datetime.now().isoformat(timespec="seconds")
            # 同日覆盖
            days = [d for d in days if d.get("date") != entry["date"]] + [entry]
            days.sort(key=lambda d: d.get("date", ""))
            cur["days"] = days
            self.store.save_feedback_stats(cur)
        return {"days": len(days), "latest": entry}

    def save_review(self, ch: int, text: str, *, summarize: bool = True) -> dict[str, Any]:
        self.store.write_feedback_review(ch, text)
        summary = _classify_reviews(text) if summarize else {}
        return {"ch": ch, "chars": len(text), "summary": summary}

    def load(self) -> dict[str, Any]:
        return self.store.load_feedback_stats()

    # ---------------- 复盘（§12.2） ----------------
    def top_drop_chapters(self, top_pct: float = 0.05) -> list[int]:
        """按弃书率找出最差的章（数据来自 stats.json 的 drop_rate）。

        缺陷 #85：stats.json 会被外部编辑器/同步盘/手改写坏。原先假设
        `days` 是 list、`chapters` 的值是 dict，实测六种畸形写法全部让
        `POST /feedback/recheck` 返回 500（`AttributeError: 'str' object has no
        attribute 'get'`），整条复盘链路不可用。数据结构不对就跳过，不抛。
        """
        stats = self.store.load_feedback_stats()
        if not isinstance(stats, dict):
            return []
        days = stats.get("days") or []
        if not isinstance(days, list):
            return []
        rows: list[tuple[int, float]] = []
        for day in days:
            if not isinstance(day, dict):
                continue
            chapters = day.get("chapters") or {}
            if not isinstance(chapters, dict):
                continue
            for k, v in chapters.items():
                try:
                    ch = int(k)
                except (TypeError, ValueError):
                    continue
                if not isinstance(v, dict):
                    continue
                try:
                    rate = float(v.get("drop_rate", 0))
                except (TypeError, ValueError):
                    continue
                rows.append((ch, rate))
        if not rows:
            return []
        # 同章取最大
        best: dict[int, float] = {}
        for ch, r in rows:
            best[ch] = max(best.get(ch, 0.0), r)
        ordered = sorted(best.items(), key=lambda kv: -kv[1])
        n = max(1, round(len(ordered) * top_pct))
        return [ch for ch, _ in ordered[:n]]

    def recheck(
        self,
        chapters: list[int] | None = None,
        *,
        top_pct: float = 0.05,
        reviews: dict[int, Any] | None = None,
    ) -> RecheckResult:
        """对高弃书率章做定向重审 + 归因 + 避雷条目提案（不直接写盘）。

        ``reviews`` 是 API 刚刚跑出的 Reviewer 报告；未传时回退到历史 review.json。
        """
        chs = chapters or self.top_drop_chapters(top_pct)
        findings: list[dict[str, Any]] = []
        proposals: list[dict[str, Any]] = []

        for ch in chs:
            review = (reviews or {}).get(ch) or self.store.load_review(ch)
            outline = self.store.load_outline(ch)
            text = self.store.read_chapter(ch)
            if not text.strip():
                continue
            cats: dict[str, int] = {}
            if review:
                for i in review.issues:
                    cats[i.category] = cats.get(i.category, 0) + 1
            attribution = _attribute(cats, text, outline)
            findings.append(
                {
                    "ch": ch,
                    "categories": cats,
                    "attribution": attribution,
                    "review_exists": review is not None,
                    "review_source": "fresh" if (reviews or {}).get(ch) is not None else "stored",
                }
            )
            for a in attribution:
                entry = f"- [第{ch}章] {a['reason']} → 后续章节避免：{a['avoid']}"
                import hashlib

                proposals.append(
                    {
                        "id": hashlib.sha1(entry.encode("utf-8")).hexdigest()[:12],
                        "ch": ch,
                        "section": "本章避雷",
                        "entry": entry,
                        "confidence": a["confidence"],
                    }
                )

        return RecheckResult(chapters=chs, findings=findings, proposals=proposals)


    def apply_proposals(self, proposals: list[dict[str, Any]]) -> dict[str, Any]:
        """把**作者确认后的**避雷条目写入 style-fingerprint.md（§12.2）。

        幂等：同一条目重复确认不会重复追加。
        """
        from ..core.store import replace_md_section

        path = self.store.p.style_fingerprint
        # 缺陷 #89：整段是「读 style-fingerprint → 去重 → 写回」，与 #88 同源。
        # 实测 20 线程并发确认不同提案，最后只剩 3 条——丢的是作者确认过的避雷条目。
        with atomic.file_lock(path):
            cur = atomic.read_text(path)
            entries = [p["entry"].strip() for p in proposals if p.get("entry", "").strip()]
            if not entries:
                return {"applied": 0, "skipped_duplicates": 0, "trimmed": 0, "limit": MAX_AVOIDANCE_ENTRIES}
            existing = _extract_section(cur, "本章避雷")
            existing_lines = {line.strip() for line in existing.splitlines() if line.strip()}
            fresh = list(dict.fromkeys(e for e in entries if e not in existing_lines))
            skipped = len(entries) - len(fresh)
            if not fresh:
                return {
                    "applied": 0,
                    "skipped_duplicates": skipped,
                    "trimmed": 0,
                    "limit": MAX_AVOIDANCE_ENTRIES,
                }
            # 缺陷 #115：这一节只增不减。每条避雷都带章号，书写到第 200 章时
            # 就是几百条；Planner._avoidance_brief 只取最近 12 条，剩下的既不
            # 生效又永远躺在真相源文件里（作者打开 style-fingerprint.md 看到一屏
            # 半年前的旧账）。上界与 #58/#68/#82 同源：留存最近的 N 条。
            keep = [line for line in existing.splitlines() if line.strip()] + fresh
            trimmed = 0
            if len(keep) > MAX_AVOIDANCE_ENTRIES:
                trimmed = len(keep) - MAX_AVOIDANCE_ENTRIES
                keep = keep[-MAX_AVOIDANCE_ENTRIES:]
            merged = "\n".join(keep)
            new = replace_md_section(cur, "本章避雷", merged.strip())
            atomic.write_text_atomic(path, new)
        return {
            "applied": len(fresh),
            "skipped_duplicates": skipped,
            "trimmed": trimmed,
            "limit": MAX_AVOIDANCE_ENTRIES,
        }


# ---------------------------------------------------------------- 归因规则

def _attribute(cats: dict[str, int], text: str, outline) -> list[dict[str, Any]]:
    """归因：钩子弱 / 节奏拖 / 期待落空 / 人设崩（§12.2）。"""
    out: list[dict[str, Any]] = []
    if cats.get("hook", 0) > 0:
        out.append(
            {
                "type": "钩子弱",
                "reason": "结尾钩子检测未通过，读者没有追更动力",
                "avoid": "章末最后 200 字内必须出现未解信息或强情绪转折",
                "confidence": 0.7,
            }
        )
    if cats.get("ai-flavor", 0) >= 2:
        out.append(
            {
                "type": "文风出戏",
                "reason": "AI 套语密度高，读者代入感下降",
                "avoid": "禁用「仿佛/宛如/不由得/眼中闪过一丝」等套语",
                "confidence": 0.8,
            }
        )
    if cats.get("repetition", 0) > 0:
        out.append(
            {
                "type": "节奏拖",
                "reason": "存在跨章重复桥段，读者觉得在水字数",
                "avoid": "同一冲突结构不得连续两章复用",
                "confidence": 0.65,
            }
        )
    if cats.get("OOC", 0) > 0 or cats.get("power", 0) > 0:
        out.append(
            {
                "type": "人设崩",
                "reason": "角色言行或战力与其设定冲突，老读者会弃书",
                "avoid": "写前必读人物卡 boundaries 与 power_scale",
                "confidence": 0.85,
            }
        )
    if not out and len(text) > 0:
        # 无审稿数据时的兜底归因：先看章末，再看信息密度
        tail = text[-300:]
        if not any(m in tail for m in ("？", "！", "突然", "……")):
            out.append(
                {
                    "type": "期待落空",
                    "reason": "章末平收，没有给出下一章的期待",
                    "avoid": "每章结尾必须留一个未解问题",
                    "confidence": 0.5,
                }
            )
        else:
            # 章末有钩子但读者仍流失 → 归因到信息密度/节奏
            quotes = sum(text.count(q) for q in ("“", "「"))
            density = quotes / max(1, len(text))
            if density < 0.004:
                out.append(
                    {
                        "type": "节奏拖",
                        "reason": f"对话密度仅 {density:.2%}，大段叙述容易劝退",
                        "avoid": "单段叙述不超过 300 字，对话占比保持 30% 以上",
                        "confidence": 0.45,
                    }
                )
            else:
                out.append(
                    {
                        "type": "期待落空",
                        "reason": "有钩子但读者仍在此章流失，可能是前期铺垫不足",
                        "avoid": "冲突要在章前 1/3 内起势，不要压到章末",
                        "confidence": 0.4,
                    }
                )
    return out


def _classify_reviews(text: str) -> dict[str, Any]:
    """章评归类摘要：好评点/差评点/槽点（轻量规则，不调模型）。"""
    good_kw = ["好看", "爽", "精彩", "喜欢", "带感", "追", "赞", "牛", "燃"]
    bad_kw = ["水", "拖", "无聊", "弃", "烂", "崩", "尬", "出戏", "看不下去", "毒"]
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    good = [l for l in lines if any(k in l for k in good_kw)]
    bad = [l for l in lines if any(k in l for k in bad_kw)]
    return {
        "total": len(lines),
        "positive": len(good),
        "negative": len(bad),
        "positive_samples": good[:5],
        "negative_samples": bad[:5],
        "sentiment": (len(good) - len(bad)) / max(1, len(lines)),
    }


def _extract_section(text: str, name: str) -> str:
    import re

    m = re.search(rf"^#+\s*{re.escape(name)}\s*$", text, re.MULTILINE)
    if not m:
        return ""
    start = m.end()
    nxt = re.search(r"^#{1,6}\s+", text[start:], re.MULTILINE)
    end = start + nxt.start() if nxt else len(text)
    return text[start:end]
