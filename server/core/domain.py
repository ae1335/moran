"""伏笔状态机 + 时间线校验 + 章节状态机（§4.2 / §6.1）。

纯逻辑，无 IO，方便单测。所有「校验」返回 issue 列表，不抛异常
（流水线需要「报警但继续」）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Literal

from .schema import (
    ACTIVE_FORESHADOW_STATUSES,
    ChapterOutline,
    CharacterCard,
    ForeshadowBook,
    ForeshadowItem,
    TimelineEntry,
)


# ================================================================ 伏笔


@dataclass
class ForeshadowIssue:
    level: Literal["error", "warn", "info"]
    code: str
    message: str
    foreshadow_id: str = ""


# 合法状态迁移（§4.2 状态机：draft → planted → hinted → resolved | dropped）
TRANSITIONS: dict[str, set[str]] = {
    "draft": {"planted", "dropped"},
    "planted": {"hinted", "resolved", "dropped"},
    "hinted": {"resolved", "dropped", "hinted"},
    "resolved": set(),
    "dropped": set(),
}


def can_transition(src: str, dst: str) -> bool:
    if src == dst:
        return True
    return dst in TRANSITIONS.get(src, set())


def apply_foreshadow_action(
    fb: ForeshadowBook, ref: str, action: str, ch: int, note: str = ""
) -> ForeshadowIssue | None:
    """把一条章纲的伏笔动作落到状态机上（就地修改）。返回问题（若有）。"""
    item = fb.get(ref)
    if item is None:
        if action == "plant":
            # 新条目自动登记为 draft 再立即 planted
            item = ForeshadowItem(id=ref, desc=note or f"（第{ch}章埋设，待补充描述）", planted_ch=ch, status="draft")
            fb.items.append(item)
        else:
            return ForeshadowIssue("error", "unknown_foreshadow", f"章纲引用了不存在的伏笔 {ref}，且动作不是 plant", ref)

    if action == "plant":
        if item.planted_ch is None:
            item.planted_ch = ch
        if item.status == "draft":
            item.status = "planted"
        return None

    if action == "hint":
        if item.status == "draft":
            return ForeshadowIssue("warn", "hint_before_plant", f"伏笔 {ref} 尚未埋设就被提示", ref)
        if not can_transition(item.status, "hinted"):
            return ForeshadowIssue("warn", "illegal_transition", f"伏笔 {ref} 状态 {item.status} 不能转 hinted", ref)
        if ch not in item.hinted_chs:
            item.hinted_chs.append(ch)
            item.hinted_chs.sort()
        item.status = "hinted"
        return None

    if action == "resolve":
        if not can_transition(item.status, "resolved"):
            return ForeshadowIssue("error", "illegal_transition", f"伏笔 {ref} 状态 {item.status} 不能转 resolved", ref)
        item.status = "resolved"
        item.resolution_ch = ch
        return None

    if action == "drop":
        if not can_transition(item.status, "dropped"):
            return ForeshadowIssue("warn", "illegal_transition", f"伏笔 {ref} 状态 {item.status} 不能转 dropped", ref)
        item.status = "dropped"
        item.dropped_reason = note or "（未说明）"
        return None

    return ForeshadowIssue("error", "unknown_action", f"未知伏笔动作 {action}", ref)


def validate_foreshadow_plan(fb: ForeshadowBook, outline: ChapterOutline) -> list[ForeshadowIssue]:
    """Planner 阶段预校验：不修改状态机。"""
    issues: list[ForeshadowIssue] = []
    for act in outline.foreshadow_actions:
        item = fb.get(act.ref)
        if item is None and act.action != "plant":
            issues.append(
                ForeshadowIssue("error", "unknown_foreshadow", f"第{outline.ch}章引用不存在的伏笔 {act.ref}", act.ref)
            )
            continue
        if item is None:
            continue
        if act.action == "hint" and item.status == "draft":
            issues.append(
                ForeshadowIssue("warn", "hint_before_plant", f"第{outline.ch}章提示未埋设的伏笔 {act.ref}", act.ref)
            )
        if act.action in ("resolve", "drop") and item.status in ("resolved", "dropped"):
            issues.append(
                ForeshadowIssue("warn", "already_closed", f"伏笔 {act.ref} 已是 {item.status}，第{outline.ch}章再次 {act.action}", act.ref)
            )
    return issues


def overdue_alerts(fb: ForeshadowBook, current_ch: int, horizon: int = 30) -> list[dict]:
    """Planner 输入：超期报警 + 临近到期（horizon 章内）。"""
    out: list[dict] = []
    for it in fb.items:
        if it.status not in ACTIVE_FORESHADOW_STATUSES:
            continue
        if it.deadline_ch is None:
            continue
        remain = it.deadline_ch - current_ch
        if remain < 0:
            out.append(
                {
                    "id": it.id,
                    "level": "overdue",
                    "desc": it.desc,
                    "deadline_ch": it.deadline_ch,
                    "overdue_by": -remain,
                    "payoff_plan": it.payoff_plan,
                    "suggestion": f"建议在第 {current_ch + 1}–{current_ch + 3} 章内埋回收动作，或标记烂尾",
                }
            )
        elif remain <= horizon:
            out.append(
                {
                    "id": it.id,
                    "level": "due_soon",
                    "desc": it.desc,
                    "deadline_ch": it.deadline_ch,
                    "overdue_by": 0,
                    "remaining": remain,
                    "payoff_plan": it.payoff_plan,
                    "suggestion": f"还有 {remain} 章到期",
                }
            )
    out.sort(key=lambda x: (x["level"] != "overdue", x.get("deadline_ch", 0)))
    return out


def active_for_context(fb: ForeshadowBook, outline: ChapterOutline, current_ch: int, horizon: int = 30) -> list[ForeshadowItem]:
    """L2 装配选择器（§5.1）：本章 action 涉及 或 deadline < 当前+horizon。"""
    touched = {a.ref for a in outline.foreshadow_actions}
    out: list[ForeshadowItem] = []
    for it in fb.items:
        if it.status not in ACTIVE_FORESHADOW_STATUSES:
            continue
        if it.id in touched:
            out.append(it)
        elif it.deadline_ch is not None and it.deadline_ch < current_ch + horizon:
            out.append(it)
    return out


# ================================================================ 时间线

DATE_RE = re.compile(r"^(.{1,6}历)\s*(\d{1,4})年\s*(\d{1,2})月\s*(\d{1,2})日$")


def parse_fiction_date(text: str) -> tuple[str, int, int, int] | None:
    """'玄历1024年三月初七' 这类中文日期 → (era, year, month, day)。

    支持阿拉伯数字与中文数字混排的月/日。
    """
    if not text:
        return None
    m = re.match(r"^(.{1,6}历)\s*(\d{1,4})年\s*(.+?)月\s*(.+?)$", text.strip())
    if not m:
        return None
    era, y, mo, dy = m.groups()
    try:
        year = int(y)
    except ValueError:
        return None
    month = cn_num(mo)
    day = cn_num(dy)
    if month is None or day is None:
        return None
    return era, year, month, day


CN_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}

#: 月份别名（网文里「正月/冬月/腊月」比「一月/十一月/十二月」更常见）
MONTH_ALIASES = {"正": 1, "冬": 11, "腊": 12, "腊月": 12, "正月": 1, "冬月": 11}


def cn_num(s: str) -> int | None:
    s = s.strip().rstrip("日")
    if not s:
        return None
    if s in MONTH_ALIASES:
        return MONTH_ALIASES[s]
    if s.startswith("初") and len(s) == 2:
        # 初七 = 7（农历前十天）
        return CN_DIGITS.get(s[1])
    if s.isdigit():
        return int(s)
    if s == "十":
        return 10
    # 廿/卅：网文里的古历写法（廿一…廿九、卅）——不支持会让「廿三日」解析失败
    if s.startswith("廿"):
        return 20 + (CN_DIGITS.get(s[1], 0) if len(s) > 1 else 0)
    if s.startswith("卅"):
        return 30 + (CN_DIGITS.get(s[1], 0) if len(s) > 1 else 0)
    if "十" in s:
        a, _, b = s.partition("十")
        tens = CN_DIGITS.get(a, 1) if a else 1
        ones = CN_DIGITS.get(b, 0) if b else 0
        return tens * 10 + ones
    if len(s) == 1:
        return CN_DIGITS.get(s)
    return None


def date_to_days(d: tuple[str, int, int, int]) -> int:
    """虚构历法：一年 360 天，一月 30 天（网文通用近似，只用于单调/差值校验）。"""
    _, y, mo, dy = d
    return y * 360 + (mo - 1) * 30 + (dy - 1)


@dataclass
class TimelineIssue:
    level: Literal["error", "warn"]
    code: str
    message: str
    ch: int = 0


def validate_timeline(entries: list[TimelineEntry]) -> list[TimelineIssue]:
    """Curator 追加前校验（§4.2）：
    1. 章号单调递增
    2. days_since_start 与日期差一致
    3. 同章内日期唯一
    """
    issues: list[TimelineIssue] = []
    seen_ch: dict[int, int] = {}
    prev: TimelineEntry | None = None
    base_days: int | None = None

    for e in sorted(entries, key=lambda x: x.ch):
        if e.ch in seen_ch:
            issues.append(TimelineIssue("warn", "dup_ch", f"第 {e.ch} 章出现多条时间线条目", e.ch))
        seen_ch[e.ch] = seen_ch.get(e.ch, 0) + 1

        parsed = parse_fiction_date(e.date)
        if parsed is None:
            if e.date:
                issues.append(TimelineIssue("warn", "bad_date", f"第 {e.ch} 章日期无法解析: {e.date}", e.ch))
            continue
        days = date_to_days(parsed)

        if prev is not None:
            prev_parsed = parse_fiction_date(prev.date)
            if prev_parsed is not None and days < date_to_days(prev_parsed):
                issues.append(
                    TimelineIssue("error", "time_reversed", f"第 {e.ch} 章日期早于第 {prev.ch} 章（{prev.date} → {e.date}）", e.ch)
                )
        if base_days is None:
            base_days = days
        expect = days - base_days
        if e.days_since_start is not None and expect != e.days_since_start:
            issues.append(
                TimelineIssue(
                    "error",
                    "days_mismatch",
                    f"第 {e.ch} 章 days_since_start={e.days_since_start}，按日期应为 {expect}",
                    e.ch,
                )
            )
        prev = e
    return issues


def normalize_timeline(entries: list[TimelineEntry]) -> list[TimelineEntry]:
    """按日期回填 days_since_start（以最早章为 0）。"""
    ordered = sorted(entries, key=lambda x: x.ch)
    base: int | None = None
    for e in ordered:
        parsed = parse_fiction_date(e.date)
        if parsed is None:
            continue
        days = date_to_days(parsed)
        if base is None:
            base = days
        e.days_since_start = days - base
    return ordered


# ================================================================ 章状态机

OUTLINE_STATUSES = ("draft", "planned", "writing", "drafted", "reviewed", "final", "published")
OUTLINE_TRANSITIONS: dict[str, set[str]] = {
    "draft": {"planned", "draft"},
    "planned": {"writing", "drafted", "planned"},
    "writing": {"drafted", "planned", "writing"},
    "drafted": {"reviewed", "writing", "drafted"},
    "reviewed": {"final", "drafted", "reviewed"},
    "final": {"published", "reviewed", "final"},
    "published": {"published", "final"},
}


def can_outline_transition(src: str, dst: str) -> bool:
    return dst in OUTLINE_TRANSITIONS.get(src, set())


# ================================================================ 战力校验


@dataclass
class PowerIssue:
    level: Literal["error", "warn"]
    message: str
    char: str = ""


def validate_power_events(outline: ChapterOutline, chars: dict[str, CharacterCard]) -> list[PowerIssue]:
    """§6.1：power_events 不得违反人物卡 power_scale.forbidden。"""
    issues: list[PowerIssue] = []
    for ev in outline.power_events:
        cid = ev.char.strip().lstrip("@")
        if cid.startswith("characters/"):
            cid = cid[len("characters/"):]
        card = chars.get(cid)
        if card is None:
            issues.append(PowerIssue("warn", f"第{outline.ch}章 power_event 引用未知角色 {ev.char}", cid))
            continue
        forb = card.power_scale.forbidden
        if forb and ev.to and ev.to in forb:
            issues.append(
                PowerIssue("error", f"第{outline.ch}章让 {card.name} 达到「{ev.to}」，违反 forbidden: {forb}", cid)
            )
    return issues


def check_beat_density(current_ch: int, beats: Iterable[str], interval: int) -> list[str]:
    """爽点间隔检查（Planner 用）：返回建议。"""
    if interval <= 0:
        return []
    if not list(beats):
        return [f"第 {current_ch} 章未标注 beat（爽点/冲突类型），建议补齐"]
    return []
