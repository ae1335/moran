"""写作统计（§11.2 面板数据）。日更、字数、节奏对标。"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

from ..core import atomic
from ..core.store import NovelStore, count_cn_words


class WritingStats:
    def __init__(self, store: NovelStore):
        self.store = store

    def overview(self) -> dict[str, Any]:
        book = self.store.load_book()
        chapters = self.store.list_chapters()
        words = [count_cn_words(self.store.read_chapter(c)) for c in chapters]
        total = sum(words)
        outlines = self.store.list_outlines()
        status_counts: dict[str, int] = {}
        for o in outlines:
            status_counts[o.status] = status_counts.get(o.status, 0) + 1

        return {
            "book": book.title,
            "chapters": len(chapters),
            "total_words": total,
            "avg_words": round(total / len(chapters), 1) if chapters else 0,
            "min_words": min(words) if words else 0,
            "max_words": max(words) if words else 0,
            "target_words_per_chapter": list(book.target.words_per_chapter),
            "progress": {
                "current": book.current_chapter,
                "target": book.target.total_chapters,
                "percent": round(book.current_chapter / max(1, book.target.total_chapters) * 100, 2),
            },
            "status_counts": status_counts,
            "outline_count": len(outlines),
        }

    def word_curve(self, last: int = 50) -> list[dict[str, Any]]:
        chapters = self.store.list_chapters()[-last:]
        return [
            {
                "ch": c,
                "words": count_cn_words(self.store.read_chapter(c)),
                "title": (self.store.load_outline(c).title if self.store.load_outline(c) else ""),
            }
            for c in chapters
        ]

    def daily_words(self, days: int = 30) -> list[dict[str, Any]]:
        """按 git 提交记录统计每天新增字数（无 git 时返回空）。"""
        from ..core.gitbot import GitBot

        gb = GitBot(self.store.root)
        log = gb.log(limit=500)
        by_day: dict[str, int] = {}
        for entry in log:
            msg = entry["message"]
            if not msg.startswith("ch-"):
                continue
            day = entry["date"][:10]
            # message 形如 "ch-0042: 淬骨 (2,143字)"
            import re

            m = re.search(r"\(([\d,]+)字\)", msg)
            if m:
                by_day[day] = by_day.get(day, 0) + int(m.group(1).replace(",", ""))
            else:
                by_day.setdefault(day, 0)
        cutoff = (dt.date.today() - dt.timedelta(days=days)).isoformat()
        return [{"date": d, "words": w} for d, w in sorted(by_day.items()) if d >= cutoff]

    def pacing_vs_benchmark(self) -> dict[str, Any]:
        """本书 vs 拆书基线的节奏对比（§11.2 与拆书基线的节奏对比图）。"""
        base = self.store.p.root / "analysis" / "benchmarks"
        if not base.exists():
            return {"available": False, "reason": "未导入对标书"}
        benchmarks: list[dict[str, Any]] = []
        for d in sorted(base.iterdir()):
            if not d.is_dir():
                continue
            s = (d / "structure.json")
            if not s.exists():
                continue
            # 缺陷 #83：必须用 atomic.read_json（读不出/半截 json 走 default），
            # 裸 json.loads(read_text()) 会让一本写坏的对标产物把整个日更面板带成 500。
            data = atomic.read_json(s, None) or {}
            benchmarks.append(
                {
                    "id": d.name,
                    "avg_words": data.get("avg_words", 0),
                    "coolpoint_curve": data.get("coolpoint_curve", []),
                }
            )
        if not benchmarks:
            return {"available": False, "reason": "对标书产物缺失"}
        return {
            "available": True,
            "book_avg_words": self.overview()["avg_words"],
            "benchmarks": benchmarks,
        }
