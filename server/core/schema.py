"""Schema 定义（§4.2）。真相源的全部核心结构。

设计原则：
- 宽容读取（旧文件缺字段不炸），严格写入（新建/校验时必填）
- 所有「模型可提议的字段」都列出，Curator patch 白名单从这里派生
"""
from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# ---------------------------------------------------------------- book.json


class BookTarget(BaseModel):
    total_chapters: int = 1200
    daily_chapters: int = 2
    words_per_chapter: tuple[int, int] = (2000, 2400)


class BookMeta(BaseModel):
    model_config = ConfigDict(extra="allow")

    title: str
    genre: str = "玄幻-东方玄幻"
    platform: str = "tomato"
    status: Literal["outline", "drafting", "hiatus", "done"] = "drafting"
    target: BookTarget = Field(default_factory=BookTarget)
    protagonist: str = "characters/mc.yaml"
    current_chapter: int = 0
    created: str = Field(default_factory=lambda: date.today().isoformat())
    # 发布管线开关（§9.2）：关闭时导出前需风险确认
    ai_flavor_pipeline: bool = True
    repetition_check: bool = True
    # 装配预算（tokens），可按模型调整
    context_budget: int = 16000
    style_ref: str | None = None


# ---------------------------------------------------------------- 角色卡 (§4.2)

CharacterRole = Literal["protagonist", "deuteragonist", "antagonist", "mentor", "support", "extra"]
CharacterStatus = Literal["active", "offstage", "dead"]


class PowerScale(BaseModel):
    model_config = ConfigDict(extra="allow")
    rank: str = ""
    ceiling: str = ""
    forbidden: str = ""


class Relation(BaseModel):
    model_config = ConfigDict(extra="allow")
    target: str
    type: str
    note: str = ""


class CharacterCard(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    name: str
    aliases: list[str] = Field(default_factory=list)
    role: str = "support"
    appearance: str = ""
    personality: list[str] = Field(default_factory=list)
    speech_habits: list[str] = Field(default_factory=list)
    boundaries: list[str] = Field(default_factory=list)
    power_scale: PowerScale = Field(default_factory=PowerScale)
    relations: list[Relation] = Field(default_factory=list)
    status: CharacterStatus = "active"
    first_chapter: int = 1
    last_chapter: int = 1


# ---------------------------------------------------------------- 伏笔状态机

ForeshadowStatus = Literal["draft", "planted", "hinted", "resolved", "dropped"]
ACTIVE_FORESHADOW_STATUSES = ("planted", "hinted")


class ForeshadowItem(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    desc: str
    planted_ch: int | None = None
    hinted_chs: list[int] = Field(default_factory=list)
    status: ForeshadowStatus = "draft"
    deadline_ch: int | None = None
    payoff_plan: str = ""
    related: list[str] = Field(default_factory=list)
    resolution_ch: int | None = None
    dropped_reason: str = ""

    def is_overdue(self, current_ch: int) -> bool:
        return (
            self.deadline_ch is not None
            and current_ch > self.deadline_ch
            and self.status in ACTIVE_FORESHADOW_STATUSES
        )


class ForeshadowBook(BaseModel):
    items: list[ForeshadowItem] = Field(default_factory=list)

    def get(self, fid: str) -> ForeshadowItem | None:
        for it in self.items:
            if it.id == fid:
                return it
        return None

    def active(self) -> list[ForeshadowItem]:
        return [i for i in self.items if i.status in ACTIVE_FORESHADOW_STATUSES]

    def overdue(self, current_ch: int) -> list[ForeshadowItem]:
        return [i for i in self.items if i.is_overdue(current_ch)]

    def next_id(self) -> str:
        n = 0
        for it in self.items:
            if it.id.startswith("F-"):
                try:
                    n = max(n, int(it.id[2:]))
                except ValueError:
                    pass
        return f"F-{n + 1:03d}"


# ---------------------------------------------------------------- 章纲 (§4.2)


class Scene(BaseModel):
    model_config = ConfigDict(extra="allow")
    loc: str = ""
    chars: list[str] = Field(default_factory=list)
    goal: str = ""
    conflict: str = ""
    outcome: str = ""


class ForeshadowAction(BaseModel):
    model_config = ConfigDict(extra="allow")
    ref: str
    action: Literal["plant", "hint", "resolve", "drop"]
    note: str = ""


class PowerEvent(BaseModel):
    model_config = ConfigDict(extra="allow")
    char: str
    field: str = "rank"
    to: str = ""
    note: str = ""


OutlineStatus = Literal["draft", "planned", "writing", "drafted", "reviewed", "final", "published"]


class ChapterOutline(BaseModel):
    model_config = ConfigDict(extra="allow", validate_assignment=True)

    ch: int
    title: str = ""
    pov: str = "mc"
    threads: list[str] = Field(default_factory=list)
    scenes: list[Scene] = Field(default_factory=list)
    must_include: list[str] = Field(default_factory=list)
    foreshadow_actions: list[ForeshadowAction] = Field(default_factory=list)
    power_events: list[PowerEvent] = Field(default_factory=list)
    hook: str = ""
    target_words: tuple[int, int] = (2000, 2400)
    status: OutlineStatus = "draft"
    style_ref: str | None = None
    beat: str = ""
    notes: str = ""

    @field_validator("target_words", mode="before")
    @classmethod
    def _coerce_pair(cls, v: Any) -> Any:
        if isinstance(v, list) and len(v) == 2:
            return tuple(v)
        return v

    @model_validator(mode="after")
    def _need_something(self) -> "ChapterOutline":
        if not self.scenes and not self.title:
            # 允许空壳草案，但至少要有个标识
            pass
        return self

    def char_refs(self) -> list[str]:
        """章纲里引用的角色 id（含 POV、场景角色、@引用）。"""
        ids: list[str] = []
        if self.pov:
            ids.append(_bare_char_id(self.pov))
        for sc in self.scenes:
            for c in sc.chars:
                ids.append(_bare_char_id(c))
        for mi in self.must_include:
            for token in _at_refs(mi):
                if token.startswith("characters/"):
                    ids.append(_bare_char_id(token))
        seen: list[str] = []
        for i in ids:
            if i and i not in seen:
                seen.append(i)
        return seen


def _bare_char_id(ref: str) -> str:
    """'characters/elder-wang' / 'mc' -> 'elder-wang' / 'mc'"""
    r = ref.strip().lstrip("@")
    if r.startswith("characters/"):
        r = r[len("characters/"):]
    if r.endswith(".yaml") or r.endswith(".yml"):
        r = r.rsplit(".", 1)[0]
    return r


def _at_refs(text: str) -> list[str]:
    """从 must_include 里抽出 @path 引用。"""
    out: list[str] = []
    for token in text.replace("，", " ").replace(",", " ").split():
        token = token.strip()
        if token.startswith("@"):
            out.append(token[1:].strip("。.,;；"))
    return out


# ---------------------------------------------------------------- 时间线


class TimelineEntry(BaseModel):
    model_config = ConfigDict(extra="allow")
    ch: int
    date: str = ""
    event: str = ""
    days_since_start: int | None = None
    note: str = ""


# ---------------------------------------------------------------- 平台 profile


class PublishPace(BaseModel):
    model_config = ConfigDict(extra="allow")
    min_daily: int = 1
    note: str = ""


class PlatformProfile(BaseModel):
    model_config = ConfigDict(extra="allow")

    platform: str
    display_name: str = ""
    chapter_words: tuple[int, int] = (2000, 2600)
    hook_required: bool = True
    hook_position: str = "last_200_words"
    hook_min_words: int = 60
    coolpoint_interval_ch: int = 3
    redline_words: list[str] = Field(default_factory=list)
    redline_file: str | None = None
    sensitive_check: Literal["off", "loose", "strict"] = "strict"
    publish_pace: PublishPace = Field(default_factory=PublishPace)
    title_max_chars: int = 20
    author_note_supported: bool = True
    notes: str = ""

    @field_validator("chapter_words", mode="before")
    @classmethod
    def _coerce_pair(cls, v: Any) -> Any:
        if isinstance(v, list) and len(v) == 2:
            return tuple(v)
        return v


# ---------------------------------------------------------------- 审稿 (§6.3)

IssueCategory = Literal[
    "OOC", "power", "timeline", "pov", "repetition", "ai-flavor", "hook", "redline", "words"
]
Severity = Literal["high", "mid", "low"]


class ReviewIssue(BaseModel):
    model_config = ConfigDict(extra="allow")
    severity: Severity = "mid"
    category: IssueCategory = "ai-flavor"
    evidence: str = ""
    suggestion: str = ""
    loc: str = ""  # 行内定位（引文片段）
    span: tuple[int, int] | None = None


class ReviewReport(BaseModel):
    model_config = ConfigDict(extra="allow")
    ch: int
    issues: list[ReviewIssue] = Field(default_factory=list)
    model: str = ""
    created: str = ""
    scope: str = "full"
    stats: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------- Curator 提案 (§6.4)

PatchOp = Literal["create", "patch", "append", "replace", "delete"]


class Proposal(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str = ""
    file: str
    op: PatchOp = "patch"
    content: str | None = None
    patch: dict[str, Any] | None = None
    section: str | None = None
    lines: list[str] | None = None
    reason: str = ""
    diff_preview: str = ""
    conflict: str = ""
    accepted: bool | None = None


class ProposalPack(BaseModel):
    model_config = ConfigDict(extra="allow")
    ch: int
    proposals: list[Proposal] = Field(default_factory=list)
    model: str = ""
    created: str = ""


# ---------------------------------------------------------------- 实体变化流 (§8.4)


class EntityDelta(BaseModel):
    model_config = ConfigDict(extra="allow")
    ch: int
    entity: str
    kind: str = "relation"  # relation | state | power | location
    op: Literal["add", "update", "remove"] = "add"
    target: str = ""
    type: str = ""
    note: str = ""


# ---------------------------------------------------------------- 用量 (§7)


class LlmCallRecord(BaseModel):
    model_config = ConfigDict(extra="allow")
    ts: str
    task: str
    provider: str
    model: str
    bridge: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    latency_ms: int = 0
    ok: bool = True
    error: str = ""
    chapter: int | None = None
    cost_yuan: float = 0.0
    fallback_hops: int = 0
    stream: bool = False


# ---------------------------------------------------------------- threads


class Thread(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str
    name: str
    kind: str = "main"  # main | romance | villain | sub
    status: Literal["active", "paused", "closed"] = "active"
    progress: str = ""
    last_ch: int = 0
    beats: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------- 书籍清单


class ShelfEntry(BaseModel):
    model_config = ConfigDict(extra="allow")
    dir: str
    title: str
    genre: str = ""
    platform: str = ""
    current_chapter: int = 0
    status: str = "drafting"
    updated: str = ""


class Shelf(BaseModel):
    model_config = ConfigDict(extra="allow")
    novels: list[ShelfEntry] = Field(default_factory=list)
