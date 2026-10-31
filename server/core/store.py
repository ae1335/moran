"""真相源读写门面（§4）。

**唯一写入口**：除本章正文外，truth/ 的任何修改必须经 Proposal → 确认 → apply。
本模块只负责「执行」写入，不负责「决定」写什么。
"""
from __future__ import annotations

import difflib
import json
import posixpath
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import yaml

from . import atomic
from .paths import NovelPaths, WorkbenchPaths, ch_name, ensure_novel_dirs, novel_dirname
from .templates import GenreTemplate, TemplateLibrary, parse_template
from .schema import (
    BookMeta,
    ChapterOutline,
    CharacterCard,
    EntityDelta,
    ForeshadowBook,
    PlatformProfile,
    Proposal,
    ReviewReport,
    Shelf,
    ShelfEntry,
    Thread,
    TimelineEntry,
)


class StoreError(RuntimeError):
    pass


# ------------------------------------------------------------------ diff 预览


def unified_diff(old: str, new: str, filename: str) -> str:
    old_lines = old.splitlines(keepends=True)
    new_lines = new.splitlines(keepends=True)
    if old_lines and not old_lines[-1].endswith("\n"):
        old_lines[-1] += "\n"
    if new_lines and not new_lines[-1].endswith("\n"):
        new_lines[-1] += "\n"
    return "".join(
        difflib.unified_diff(old_lines, new_lines, fromfile=f"a/{filename}", tofile=f"b/{filename}", n=3)
    )


# ------------------------------------------------------------------ markdown 分节


SECTION_RE = re.compile(r"^(#{1,6})\s+(.*)$", re.MULTILINE)


def _md_headings(text: str) -> list:
    """扫出 md 里的标题，**跳过围栏代码块内的**。

    缺陷 #102：SECTION_RE 只看行首的 #，会把围栏代码块里的注释行当成标题，
    于是替换一个恰好跟代码同名的分节时，围栏被拦腰截断 —— 代码块后半段变成
    「分节正文」被替换掉，文档直接写坏。
    """
    out = []
    fence = False
    pos = 0
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped.startswith("```"):
            fence = not fence
        elif not fence:
            m = SECTION_RE.match(line)
            if m:
                out.append((pos, len(m.group(1)), m.group(2).strip()))
        pos += len(line) + 1
    return out


def replace_md_section(text: str, section: str, new_body: str) -> str:
    """把 md 里某个标题（任意层级，按标题文本匹配）下的内容替换掉。

    找不到该标题则追加到文末新建同层级标题。
    同名标题出现多次时**全部替换**（缺陷 #102）——只替换第一个会让第二个同名
    分节静默留在文档里，作者以为改过了其实没改。
    """
    matches = _md_headings(text)
    want = section.strip()
    hits = [i for i, h in enumerate(matches) if h[2] == want]

    if not hits:
        level = 2
        sep = "" if text.endswith("\n\n") or not text.strip() else ("\n" if text.endswith("\n") else "\n\n")
        return f"{text}{sep}{'#' * level} {section}\n\n{new_body.strip()}\n"

    level = matches[hits[0]][1]
    # 从后往前替换，下标不会因前面的改动作废
    for idx in reversed(hits):
        start = matches[idx][0]
        end = len(text)
        for j in range(idx + 1, len(matches)):
            if matches[j][1] <= level:
                end = matches[j][0]
                break
        head = text[start:].split("\n", 1)[0]
        text = f"{text[:start]}{head}\n\n{new_body.strip()}\n\n{text[end:]}".replace("\n\n\n\n", "\n\n\n")
        matches = _md_headings(text)
        hits = [i for i, h in enumerate(matches) if h[2] == want]
        hits = [h for h in hits if h >= 0][: idx + 1]
    return text


# ------------------------------------------------------------------ NovelStore


class NovelStore:
    """一本书的读写门面。"""

    def __init__(self, paths: NovelPaths, shared_templates: Path | None = None):
        self.p = paths
        # 工作区级共享模板库（M8「共享模板」）。由 Workbench 注入；
        # 直接构造 NovelStore 时为空，模板功能降级为「只用书内镜像」。
        self.shared_templates = Path(shared_templates) if shared_templates else None

    def workbench_template_library(self) -> TemplateLibrary:
        """题材模板库：工作区共享优先，书内 analysis/templates 兜底。"""
        book_dir = self.p.root / "analysis" / "templates"
        shared = self.shared_templates or book_dir
        return TemplateLibrary(shared, book_dir)

    # ---------------- 基础 ----------------
    @property
    def root(self) -> Path:
        return self.p.root

    def exists(self) -> bool:
        return self.p.book_json.exists()

    def rel(self, path: Path) -> str:
        return self.p.rel(path)

    def resolve(self, relpath: str) -> Path:
        """安全解析书内相对路径，拒绝逃逸。"""
        relpath = relpath.strip().lstrip("/")
        relpath = relpath.replace("\\", "/")
        if relpath.startswith("@") and relpath[1:] not in ("chapters", "memory"):
            # @ 前缀是章纲里的引用语法，允许 truth 前缀省略
            cand = relpath[1:]
            for prefix in ("", "truth/", "materials/", "outline/"):
                if (self.p.root / prefix / cand).exists():
                    relpath = f"{prefix}{cand}" if prefix else cand
                    break
            else:
                relpath = relpath[1:]
        target = (self.p.root / relpath).resolve()
        root = self.p.root.resolve()
        if target != root and root not in target.parents:
            raise StoreError(f"path escapes novel root: {relpath}")
        return target

    # ---------------- book.json ----------------
    def load_book(self) -> BookMeta:
        raw = atomic.read_json(self.p.book_json)
        if raw is None:
            raise StoreError(f"book.json 不存在: {self.p.book_json}")
        return BookMeta.model_validate(raw)

    def save_book(self, book: BookMeta) -> None:
        atomic.write_json_atomic(self.p.book_json, book.model_dump(mode="json"))

    # ---------------- 角色卡 ----------------
    def list_characters(self) -> list[CharacterCard]:
        cards: list[CharacterCard] = []
        for f in self.p.iter_character_files():
            raw = atomic.read_yaml(f) or {}
            if not isinstance(raw, dict):
                continue
            raw.setdefault("id", f.stem)
            raw.setdefault("name", f.stem)
            cards.append(CharacterCard.model_validate(raw))
        return cards

    def get_character(self, cid: str) -> CharacterCard | None:
        cid = cid.strip().lstrip("@")
        if cid.startswith("characters/"):
            cid = cid[len("characters/"):]
        if cid.endswith((".yaml", ".yml")):
            cid = cid.rsplit(".", 1)[0]
        f = self.p.characters_dir / f"{cid}.yaml"
        if not f.exists():
            for c in self.list_characters():
                if c.id == cid or c.name == cid or cid in c.aliases:
                    return c
            return None
        raw = atomic.read_yaml(f) or {}
        # 缺陷 #94：角色卡被写成标量/列表时 setdefault 会抛 AttributeError
        # （实测 `get_character` 直接崩，审稿与上下文装配都会受影响）。
        if not isinstance(raw, dict):
            raw = {}
        raw.setdefault("id", cid)
        raw.setdefault("name", cid)
        return CharacterCard.model_validate(raw)

    def save_character(self, card: CharacterCard) -> Path:
        f = self.p.characters_dir / f"{_assert_plain_stem(card.id, '角色 id')}.yaml"
        return atomic.write_yaml_atomic(f, card.model_dump(mode="json", exclude_none=True))

    def character_map(self) -> dict[str, CharacterCard]:
        out: dict[str, CharacterCard] = {}
        for c in self.list_characters():
            out[c.id] = c
            for a in c.aliases:
                out.setdefault(a, c)
        return out

    # ---------------- world/ + constitution ----------------
    def world_file(self, name: str) -> Path:
        return self.p.world_dir / name

    def list_world_files(self) -> list[str]:
        if not self.p.world_dir.exists():
            return []
        return sorted(p.name for p in self.p.world_dir.iterdir() if p.is_file())

    def read_truth_text(self, name: str) -> str:
        """name: constitution.md / world/power-system.md / style-fingerprint.md / arcs.md"""
        name = name.strip().lstrip("/").lstrip("@")
        candidates = [self.p.truth / name, self.p.root / name]
        for c in candidates:
            if c.exists():
                return atomic.read_text(c)
        return ""

    # ---------------- 伏笔 ----------------
    def load_foreshadow(self) -> ForeshadowBook:
        raw = atomic.read_json(self.p.foreshadow, {"items": []})
        if isinstance(raw, list):
            raw = {"items": raw}
        return ForeshadowBook.model_validate(raw or {"items": []})

    def save_foreshadow(self, fb: ForeshadowBook) -> None:
        atomic.write_json_atomic(self.p.foreshadow, fb.model_dump(mode="json"))

    # ---------------- 时间线 ----------------
    def load_timeline(self) -> list[TimelineEntry]:
        raw = atomic.read_yaml(self.p.timeline) or []
        if not isinstance(raw, list):
            return []
        out: list[TimelineEntry] = []
        for item in raw:
            if isinstance(item, dict) and "ch" in item:
                out.append(TimelineEntry.model_validate(item))
        return sorted(out, key=lambda e: e.ch)

    def save_timeline(self, entries: Iterable[TimelineEntry]) -> None:
        atomic.write_yaml_atomic(self.p.timeline, [e.model_dump(mode="json", exclude_none=True) for e in entries])

    # ---------------- 章纲 ----------------
    def load_outline(self, ch: int) -> ChapterOutline | None:
        raw = atomic.read_json(self.p.outline_json(ch))
        if raw is None:
            return None
        # 缺陷 #94：章纲文件被外部工具写成 `"oops"` / `[1,2]` 时，`raw.setdefault` 会抛
        # AttributeError（实测 `GET /outline/1` 直接 500）。读不出 dict 就当没有章纲。
        if not isinstance(raw, dict):
            return None
        raw.setdefault("ch", ch)
        return ChapterOutline.model_validate(raw)

    def save_outline(self, outline: ChapterOutline) -> Path:
        return atomic.write_json_atomic(self.p.outline_json(outline.ch), outline.model_dump(mode="json"))

    def list_outlines(self) -> list[ChapterOutline]:
        out: list[ChapterOutline] = []
        for n in self.p.iter_outline_numbers():
            o = self.load_outline(n)
            if o:
                out.append(o)
        return out

    # ---------------- 正文 ----------------
    def read_chapter(self, ch: int) -> str:
        return atomic.read_text(self.p.chapter_md(ch))

    def write_chapter(self, ch: int, text: str) -> Path:
        return atomic.write_text_atomic(self.p.chapter_md(ch), text)

    def chapter_exists(self, ch: int) -> bool:
        return self.p.chapter_md(ch).exists()

    def list_chapters(self) -> list[int]:
        return list(self.p.iter_chapter_numbers())

    def chapter_word_count(self, ch: int) -> int:
        return count_cn_words(self.read_chapter(ch))

    # ---------------- 版本 ----------------
    def list_versions(self, ch: int) -> list[dict[str, Any]]:
        d = self.p.chapter_versions_dir(ch)
        if not d.exists():
            return []
        out: list[dict[str, Any]] = []
        for f in sorted(d.glob("v*.md"), key=lambda p: int(re.sub(r"\D", "", p.stem) or 0)):
            n = int(re.sub(r"\D", "", f.stem) or 0)
            meta_path = self.p.chapter_version_meta(ch, n)
            meta = atomic.read_json(meta_path, {}) or {}
            text = f.read_text(encoding="utf-8")
            out.append(
                {
                    "version": n,
                    "path": self.rel(f),
                    "words": count_cn_words(text),
                    "created": meta.get("created", ""),
                    "model": meta.get("model", ""),
                    "score": meta.get("score"),
                    "adopted": meta.get("adopted", False),
                    "note": meta.get("note", ""),
                }
            )
        return out

    def write_version(self, ch: int, v: int, text: str, meta: dict[str, Any] | None = None) -> None:
        atomic.write_text_atomic(self.p.chapter_version(ch, v), text)
        atomic.write_json_atomic(self.p.chapter_version_meta(ch, v), meta or {})

    def save_version_meta(self, ch: int, v: int, meta: dict[str, Any]) -> None:
        atomic.write_json_atomic(self.p.chapter_version_meta(ch, v), meta)

    def next_version_number(self, ch: int) -> int:
        vs = self.list_versions(ch)
        return (max((v["version"] for v in vs), default=0)) + 1

    # ---------------- memory ----------------
    def read_summary(self, ch: int) -> str:
        return atomic.read_text(self.p.summary_md(ch))

    def write_summary(self, ch: int, text: str) -> Path:
        return atomic.write_text_atomic(self.p.summary_md(ch), text)

    def write_assembled(self, ch: int, text: str) -> Path:
        return atomic.write_text_atomic(self.p.assembled_md(ch), text)

    def load_review(self, ch: int) -> ReviewReport | None:
        raw = atomic.read_json(self.p.review_json(ch))
        if raw is None:
            return None
        # 缺陷 #86：手改/同步盘可能把 review.json 写成数组或标量，
        # 原先直接 setdefault() 会抛 `AttributeError: 'list' object has no attribute
        # 'setdefault'`，实测让 POST /feedback/recheck 直接 500。读不出来就当没审过。
        if not isinstance(raw, dict):
            return None
        raw.setdefault("ch", ch)
        return ReviewReport.model_validate(raw)

    def save_review(self, report: ReviewReport) -> Path:
        return atomic.write_json_atomic(self.p.review_json(report.ch), report.model_dump(mode="json"))

    def append_entity_delta(self, delta: EntityDelta) -> None:
        atomic.append_line_atomic(self.p.entity_deltas, json.dumps(delta.model_dump(mode="json"), ensure_ascii=False))

    def read_entity_deltas(self) -> list[EntityDelta]:
        return self.read_entity_deltas_report().rows

    def read_entity_deltas_report(self):
        """读实体增量，并如实报告「有多少没读出来」（缺陷 #118）。

        ``memory/entity-deltas.jsonl`` 是真相源且进版本库：git 合并冲突、写入中断、
        编码错乱都会让它部分失效。旧实现静默跳过坏行，实体图谱从 13 个掉到 4 个
        而索引重建/对账依旧全绿。这里保留「坏行跳过」的容错语义，但把跳过了什么
        回传给调用方（``index.build`` / ``index.verify``），让它变成看得见的告警。
        """
        rep = atomic.read_jsonl_report(self.p.entity_deltas)
        rows: list[EntityDelta] = []
        bad: list[int] = []
        for i, raw in enumerate(rep.rows, start=1):
            try:
                rows.append(EntityDelta.model_validate(raw))
            except Exception:
                bad.append(i)
        rep.rows = rows  # type: ignore[assignment]
        rep.bad_lines = sorted(set(rep.bad_lines) | set(bad))
        return rep

    # ---------------- threads ----------------
    def load_threads(self) -> list[Thread]:
        out: list[Thread] = []
        d = self.p.threads_dir
        if not d.exists():
            return out
        for f in sorted(d.glob("*.json")):
            raw = atomic.read_json(f)
            if not isinstance(raw, dict):
                continue
            raw.setdefault("id", f.stem)
            raw.setdefault("name", f.stem)
            out.append(Thread.model_validate(raw))
        return out

    def save_thread(self, t: Thread) -> Path:
        stem = _assert_plain_stem(t.id, "线程 id")
        return atomic.write_json_atomic(self.p.threads_dir / f"{stem}.json", t.model_dump(mode="json"))

    # ---------------- profiles ----------------
    def load_profile(self, platform: str) -> PlatformProfile:
        # 缺陷 #80：platform 会被拼成 profiles/{platform}.json，必须是没有分隔符的纯名字。
        # 否则 platform=../../wsroot 会读到书外任意 json 并把它当平台配置用
        # （探针实测：预检的字数下限从 2000 变成书外文件里的 10，红线词库同理）。
        stem = _assert_plain_stem(platform, "平台名")
        f = self.p.profiles / f"{stem}.json"
        raw = atomic.read_json(f)
        if raw is None:
            return PlatformProfile(platform=platform)
        # 缺陷 #94：profile 被写成标量/列表时 setdefault 会抛 AttributeError，
        # 实测 `GET /export/precheck` 直接 500。读不出 dict 就当没配过（走默认值）。
        if not isinstance(raw, dict):
            return PlatformProfile(platform=platform)
        raw.setdefault("platform", platform)
        prof = PlatformProfile.model_validate(raw)
        # redline 词库外置文件
        if prof.redline_file:
            # 缺陷 #80：外置红线词库同样要挡在书根内（resolve 会抛 StoreError）。
            extra = self.resolve(prof.redline_file)
            if extra.exists():
                # 缺陷 #90：红线词库是作者手抄的 txt，混进二进制字节时裸 read_text 会抛
                # UnicodeDecodeError，而它是 ValueError 子类，会被 #66 的全局 handler
                # 接走变成一串英文 codec 报错（实测 export/precheck 直接 400）。
                # 词库读不出来就按「没有外置词库」处理，不阻断。
                raw = atomic.read_text(extra, "")
                words = [w.strip() for w in raw.splitlines() if w.strip()]
                prof.redline_words = list(dict.fromkeys([*prof.redline_words, *words]))
        return prof

    def list_profiles(self) -> list[str]:
        if not self.p.profiles.exists():
            return []
        return sorted(p.stem for p in self.p.profiles.glob("*.json"))

    def save_profile(self, prof: PlatformProfile) -> Path:
        stem = _assert_plain_stem(prof.platform, "平台名")
        # 缺陷 #80：外置红线词库也要在保存时就挡住，别等读取时才炸（作者手改 profile 也走这里）
        if prof.redline_file:
            self.resolve(prof.redline_file)
        return atomic.write_json_atomic(self.p.profiles / f"{stem}.json", prof.model_dump(mode="json"))

    # ---------------- feedback ----------------
    def load_feedback_stats(self) -> dict[str, Any]:
        return atomic.read_json(self.p.feedback_stats, {}) or {}

    def save_feedback_stats(self, data: dict[str, Any]) -> Path:
        return atomic.write_json_atomic(self.p.feedback_stats, data)

    def read_feedback_review(self, ch: int) -> str:
        return atomic.read_text(self.p.feedback_review(ch))

    def write_feedback_review(self, ch: int, text: str) -> Path:
        return atomic.write_text_atomic(self.p.feedback_review(ch), text)

    # ---------------- 提案落盘 ----------------
    def apply_proposal(self, prop: Proposal) -> str:
        """执行一条提案。返回 diff 文本（供审计）。

        缺陷 #103/#104：提案的落盘面此前完全没有白名单 —— `store.resolve()` 只挡「逃出
        书根」，而书根内除了真相源还有 book.json（书架唯一索引）、chapters/*.md、
        outline/*.json。实测 `op=delete` 打这几个都直接删掉（book.json 一删这本书从书架
        消失）；`op=replace` 同样能把 book.json 的 title 改掉、把正文整章覆盖。
        PROJECT.md §2 承诺白名单是「物理保证，不是靠 prompt 约束」，而它此前只挂在
        Curator 的**生成端**（_to_proposals）——执行端不查，等于绕过生成端就没人拦。
        这里把门槛下沉到落盘这一步：四类 op 全过 PROPOSAL_FILE_PREFIXES。
        """
        target = self.resolve(prop.file)
        _assert_proposal_file_allowed(prop.file, prop.op)
        # 缺陷 #117：apply 本身是「读旧内容 → 拼 → 写回」，四条并发提案各自读到同一份
        # old 后互相覆盖 —— 实测 12 条并发 append 只活 6 条，timeline.yaml 只活 4 条，
        # 全程无异常（响应全 200）。整段必须持锁，锁按目标文件粒度，不影响别的文件并发。
        with atomic.file_lock(target):
            if prop.op == "delete":
                old = atomic.read_text(target) if target.exists() else ""
                if target.exists():
                    target.unlink()
                return unified_diff(old, "", prop.file)

            old = atomic.read_text(target) if target.exists() else ""
            new = self._compute_new_content(prop, target, old)

            atomic.write_text_atomic(target, new)
            return unified_diff(old, new, prop.file)

    def preview_proposal(self, prop: Proposal) -> str:
        target = self.resolve(prop.file)
        old = atomic.read_text(target) if target.exists() else ""
        try:
            new = self._compute_new_content(prop, target, old)
        except Exception as exc:  # 预览失败不应炸整个提案包
            return f"[预览失败] {exc}"
        if prop.op == "delete":
            new = ""
        return unified_diff(old, new, prop.file)

    def _compute_new_content(self, prop: Proposal, target: Path, old: str) -> str:
        suffix = target.suffix.lower()
        # 缺陷 #100：replace / create 的 content 缺省是 None，prop.content or "" 会把真相源整段清空。
        # 实测：模型提案 {"op":"replace"} 不带 content → foreshadow.json 被写成 0 字节，
        # 作者手上的伏笔台账凭空消失。要清空请用 op=delete（那是显式意图）。
        if prop.op in ("create", "replace"):
            body = prop.content
            if body is None or not body.strip():
                raise ValueError(
                    f"{prop.op} 提案缺少 content（目标 {prop.file}）：不允许用空内容覆盖真相源，如需清空请用 op=delete"
                )
            return body
        if prop.op == "delete":
            return ""

        if prop.op == "append":
            if suffix == ".jsonl":
                lines = prop.lines or ([prop.content] if prop.content else [])
                body = "".join(l if l.endswith("\n") else l + "\n" for l in lines if l is not None)
                if old and not old.endswith("\n"):
                    old += "\n"
                return old + body
            if suffix in (".yaml", ".yml"):
                cur = yaml.safe_load(old) if old.strip() else []
                if not isinstance(cur, list):
                    cur = [cur] if cur else []
                add = prop.patch if isinstance(prop.patch, dict) else None
                if add is None and prop.content:
                    add = yaml.safe_load(prop.content)
                if isinstance(add, list):
                    cur.extend(add)
                elif add:
                    cur.append(add)
                return yaml.safe_dump(cur, allow_unicode=True, sort_keys=False, default_flow_style=False)
            if suffix == ".json":
                # 缺陷 #101：append 打 .json 以前会掉进下面的 md/txt 分支，把正文追加在 JSON 后面。
                # 实测：原本合法的 JSON 后面被追加一行中文，文件再也不是合法 JSON，
                # 再 load_foreshadow() 就成了空台账 —— 等于静默丢掉作者的全部伏笔。
                # 给 patch（字典）的 append 走深合并，其余一律拒绝，别让坏数据落盘。
                if isinstance(prop.patch, dict):
                    cur = json.loads(old) if old.strip() else {}
                    return json.dumps(_deep_merge(cur, prop.patch), ensure_ascii=False, indent=2) + "\n"
                raise ValueError(
                    f"append 目标 {prop.file} 是 .json：请用 op=patch 提交字典，或改用 .jsonl + lines 追加"
                )
            # md / txt
            sep = "" if not old or old.endswith("\n\n") else ("\n" if old.endswith("\n") else "\n\n")
            return f"{old}{sep}{(prop.content or '').rstrip()}\n"

        # patch
        if suffix in (".json",):
            cur = json.loads(old) if old.strip() else {}
            patch = dict(prop.patch or {})
            # §6.4 的伏笔 patch 形如 {"F-001": {"status": "hinted"}}，按 id 合并进 items
            if target.name == "foreshadow.json" and _looks_like_id_map(patch):
                patch = {"items": [{"id": k, **(v if isinstance(v, dict) else {})} for k, v in patch.items()]}
            cur = _deep_merge(cur, patch)
            return json.dumps(cur, ensure_ascii=False, indent=2) + "\n"
        if suffix in (".yaml", ".yml"):
            cur = yaml.safe_load(old) if old.strip() else {}
            if not isinstance(cur, (dict, list)):
                cur = {}
            cur = _deep_merge(cur, prop.patch or {})
            return yaml.safe_dump(cur, allow_unicode=True, sort_keys=False, default_flow_style=False)
        # md 分节替换
        if prop.section:
            return replace_md_section(old, prop.section, prop.content or "")
        return prop.content if prop.content is not None else old


def _deep_merge(base: Any, patch: Any) -> Any:
    """dict 深合并；None 表示删除该 key。

    list 的合并规则（Curator patch 的关键）：
    - 若两边都是 list 且元素是带唯一键的 dict（id/key/ref），按 key 合并到既有条目；
      patch 里出现新 key 则追加；条目带 {"_delete": true} 则删除。
    - 否则整体替换。
    """
    if isinstance(base, dict) and isinstance(patch, dict):
        out = dict(base)
        for k, v in patch.items():
            if v is None:
                out.pop(k, None)
            elif k in out:
                out[k] = _deep_merge(out[k], v)
            else:
                out[k] = v
        return out
    if isinstance(base, list) and isinstance(patch, list):
        key = _list_key(base, patch)
        if key is None:
            return patch
        out = [dict(x) if isinstance(x, dict) else x for x in base]
        index = {x.get(key): i for i, x in enumerate(out) if isinstance(x, dict) and key in x}
        for item in patch:
            if not isinstance(item, dict) or key not in item:
                out.append(item)
                continue
            ident = item[key]
            if ident in index:
                i = index[ident]
                if item.get("_delete"):
                    out[i] = None  # 标记删除
                else:
                    out[i] = _deep_merge(out[i], {k: v for k, v in item.items()})
            else:
                if not item.get("_delete"):
                    index[ident] = len(out)
                    out.append(dict(item))
        return [x for x in out if x is not None]
    return patch


def _looks_like_id_map(patch: dict) -> bool:
    """{"F-001": {...}, "F-002": {...}} 形态（键是业务 id，值全是 dict）。"""
    if not patch:
        return False
    if any(k in ("items", "held", "novels") for k in patch):
        return False
    return all(isinstance(v, dict) for v in patch.values()) and all(
        isinstance(v, dict) for v in patch.values()
    )


def _list_key(base: list, patch: list) -> str | None:
    """判断两个 list 是否该按某唯一键合并。"""
    if not base or not patch:
        return None
    if not all(isinstance(x, dict) for x in base) or not all(isinstance(x, dict) for x in patch):
        return None
    for cand in ("id", "key", "ref", "name"):
        if all(cand in x for x in base) and any(cand in x for x in patch):
            return cand
    return None


# ------------------------------------------------------------------ 中文词数


_CJK = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")
_WORD = re.compile(r"[A-Za-z0-9]+")


def count_cn_words(text: str) -> int:
    """中文按字计，英文/数字按词计（与网文平台字数口径接近）。"""
    if not text:
        return 0
    # 去掉 markdown 语法与标题行标记，避免把 # 计入
    body = re.sub(r"^#{1,6}\s*", "", text, flags=re.MULTILINE)
    return len(_CJK.findall(body)) + len(_WORD.findall(body))


# 提案可触碰的真相源前缀（§6.4）。Curator 的 ALLOWED_PREFIXES 是它的别名；
# 放在 core 是为了让「删除面白名单」这条硬约束不依赖 agents 层（架构铁律 #54）。
PROPOSAL_FILE_PREFIXES = (
    "memory/",
    "truth/foreshadow.json",
    "truth/timeline.yaml",
    "truth/world-state.md",
    "truth/characters/",
    "truth/resources.json",
    "truth/arcs.md",
    "truth/style-fingerprint.md",
    "outline/threads/",
)





def _assert_proposal_file_allowed(path: str, op: str = "") -> str:
    """提案只能落在 PROPOSAL_FILE_PREFIXES 里（缺陷 #103/#104）。

    删除与写入都查：删除不可逆；写入能把 book.json 的 title 改掉、把正文整章覆盖。
    作者自己改正文走 PUT /chapters（不经提案），不受此限。

    这张表定义在 core 而不是 agents：curator 的 ALLOWED_PREFIXES 就是它的别名，
    反过来让 core 去 import agents 会踩 README 的架构铁律（#54 会立即红）。
    """
    norm = posixpath.normpath(str(path or "").strip().lstrip("/").replace("\\", "/"))
    verb = "删除" if op == "delete" else "写入"
    if norm in (".", "..") or norm.startswith("../"):
        raise StoreError(f"提案不允许{verb} {path!r}")
    if not any(norm == p or norm.startswith(p) for p in PROPOSAL_FILE_PREFIXES):
        allowed = "、".join(sorted(PROPOSAL_FILE_PREFIXES))
        raise StoreError(f"提案不允许{verb} {path!r}：白名单只允许 {allowed}")
    return norm


def _assert_plain_stem(name: str, what: str) -> str:
    """文件名（目录名 / id / 平台名）必须是单段名：不含路径分隔符、不是 . 或 ..。

    缺陷 #77：凡是用用户输入拼 `dir / f"{name}.json"` 的地方都有同一类风险——
    ``save_thread`` 收到 `id="../../evil"` 会写到书根，收到
    ``id="../../../../../tmp/x/evil"`` 甚至能写出书外（探针实测文件真的落盘了），
    而 `id="../../book"` 会直接覆盖 book.json，整本书从书架消失。
    这类校验必须落在 store 层：调用方（路由 / CLI / 模板初始化）各自记一遍必漏。
    """
    if not isinstance(name, str) or not name or name.strip() != name or name in (".", ".."):
        raise StoreError(f"非法{what}: {name!r}")
    if "/" in name or chr(92) in name:
        raise StoreError(f"非法{what}（不能含路径分隔符）: {name!r}")
    return name


def _assert_plain_dirname(name: str) -> None:
    """书目录名必须是单段名，不含 / .. 或空。

    「以磁盘为准」的查找（get_store/find_store）不会拒绝越界目录名：
    ``novels/../../outside`` 只要里面摆了 book.json 就能被打开。当前 API 层
    （routes._book）因为 Starlette 会先折叠路径而碰不到这条路径，但纵深防御
    不能依赖调用方的框架行为——一旦有人在别处直接调 get_store，越界就成真。
    """
    _assert_plain_stem(name, "书目录名")


# ------------------------------------------------------------------ Workbench


class Workbench:
    """工作区级门面：书架 + 建书 + 全局配置。"""

    def __init__(self, root: Path):
        self.paths = WorkbenchPaths(Path(root))
        self.paths.ensure()

    # ---- 书架 ----
    def load_shelf(self) -> Shelf:
        raw = atomic.read_json(self.paths.shelf, {"novels": []}) or {"novels": []}
        if isinstance(raw, list):
            raw = {"novels": raw}
        shelf = Shelf.model_validate(raw)
        # 以磁盘为准，顺手清理已删除的书
        alive: list[ShelfEntry] = []
        for e in shelf.novels:
            if (self.paths.novels / e.dir / "book.json").exists():
                alive.append(e)
        shelf.novels = alive
        return shelf

    def save_shelf(self, shelf: Shelf) -> None:
        atomic.write_json_atomic(self.paths.shelf, shelf.model_dump(mode="json"))

    def refresh_shelf(self) -> Shelf:
        """扫描 novels/ 重建书架（外部新建的书也能被发现）。"""
        shelf = self.load_shelf()
        known = {e.dir: e for e in shelf.novels}
        out: list[ShelfEntry] = []
        if self.paths.novels.exists():
            for d in sorted(self.paths.novels.iterdir()):
                if not d.is_dir() or not (d / "book.json").exists():
                    continue
                np = NovelPaths(d)
                store = NovelStore(np, self.paths.shared_templates)
                try:
                    book = store.load_book()
                except Exception:
                    continue
                entry = known.get(d.name) or ShelfEntry(dir=d.name, title=book.title)
                entry.title = book.title
                entry.genre = book.genre
                entry.platform = book.platform
                entry.status = book.status
                entry.current_chapter = book.current_chapter
                entry.updated = _mtime_iso(d / "book.json")
                out.append(entry)
        shelf.novels = out
        self.save_shelf(shelf)
        return shelf

    # ---- 题材模板（§10.2 / M8 共享模板） ----
    def template_library(self, np: NovelPaths | None = None) -> TemplateLibrary:
        book_dir = (np.root / "analysis" / "templates") if np is not None else None
        return TemplateLibrary(self.paths.shared_templates, book_dir)

    def _resolve_template(self, template: Any, book: BookMeta) -> GenreTemplate | None:
        lib = self.template_library()
        if isinstance(template, GenreTemplate):
            return template
        if isinstance(template, dict):
            return parse_template(template, id=str(template.get("id") or "inline"))
        if isinstance(template, str) and template.strip():
            return lib.get(template.strip())
        # 未指定 → 按题材自动匹配（宁缺毋滥，匹配不到就走内置种子）
        return lib.match_genre(book.genre)

    # ---- 建书 ----
    def create_novel(self, book: BookMeta, template: Any = None) -> NovelStore:
        """建书并播种真相源。

        ``template``（§10.2 / M8）：模板 id、题材名、GenreTemplate 或原始 dict。
        为 None 时按 ``book.genre`` 自动挑一份最接近的题材模板；都没有则用内置种子。

        缺陷 #60：书名长度在``novel_dirname``里就已经被挡了，这里再兜一层是
        为了让调用方拿到的是 ``StoreError``（HTTP 层转 409）而不是穿透出来的
        ``OSError: [Errno 36] File name too long``（HTTP 层会变成看不懂的 500）。
        """
        try:
            dirname = novel_dirname(book.title)
        except ValueError as exc:
            raise StoreError(str(exc)) from exc
        # 缺陷 #78：平台名会直接拼进 profiles/ 与 workbench/profiles/ 的文件名。
        # 建书时不过一遍，../../evil 会把 profile 写到工作区根甚至书外
        # （实测 ws/evil.json、workbench/profiles/a/b.json 都真的落盘了）。
        _assert_plain_stem(str(book.platform or ""), "平台名")
        np = self.paths.novel(dirname)
        if np.book_json.exists():
            raise StoreError(f"书已存在: {dirname}")
        tpl = self._resolve_template(template, book)
        ensure_novel_dirs(np)
        store = NovelStore(np, self.paths.shared_templates)
        store.save_book(book)
        _seed_truth(store, book, template=tpl)
        # 默认平台 profile
        if not (np.profiles / f"{book.platform}.json").exists():
            _seed_profile(store, book.platform, self.paths)
        self.refresh_shelf()
        return store

    def get_store(self, dirname: str) -> NovelStore:
        _assert_plain_dirname(dirname)
        np = self.paths.novel(dirname)
        if not np.book_json.exists():
            raise StoreError(f"书不存在: {dirname}")
        return NovelStore(np, self.paths.shared_templates)

    def find_store(self, key: str) -> NovelStore:
        """按目录名或书名查找。

        key 可以是目录名也可以是书名，所以入口不按目录名校验；真正的守住
        在 get_store（两条分支最终都经过它）。书名含 / 是合法的（目录名经
        safe_title 净化，书名没有）。
        """
        if (self.paths.novels / key / "book.json").exists():
            return self.get_store(key)
        for e in self.load_shelf().novels:
            if e.title == key or e.dir == key:
                return self.get_store(e.dir)
        raise StoreError(f"找不到书: {key}")


def _mtime_iso(path: Path) -> str:
    import datetime as _dt

    try:
        return _dt.datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")
    except OSError:
        return ""


# ------------------------------------------------------------------ 种子内容


def _template_blocks(tpl: GenreTemplate | None) -> dict[str, str]:
    """把题材模板翻译成种子文本里的几段话（§10.2：开局模式/金手指/节奏基线）。

    模板为空时返回空 dict，调用方走内置默认文案——**绝不因为模板缺失改坏种子**。
    """
    if tpl is None:
        return {}
    out: dict[str, str] = {}
    if tpl.opening_pattern:
        out["opening"] = f"- 开局模式（对标模板 {tpl.id or tpl.genre}）：{tpl.opening_pattern}"
    if tpl.golden_finger:
        out["golden_finger"] = f"- 金手指设定：{tpl.golden_finger}"
    if tpl.opening_hook_type:
        out["hook"] = f"- 主导断章钩型：{tpl.opening_hook_type}"
    if tpl.arc_length_ch:
        out["arc"] = f"- 单卷章数基线：约 {tpl.arc_length_ch} 章"
    if tpl.power_up_interval_ch:
        out["power"] = f"- 升级节奏基线：约每 {tpl.power_up_interval_ch} 章一次"
    if tpl.coolpoint_interval_ch:
        out["coolpoint"] = f"- 爽点间隔基线：不超过 {tpl.coolpoint_interval_ch} 章"
    return out


def _block(blocks: dict[str, str], *keys: str) -> str:
    lines = [blocks[k] for k in keys if blocks.get(k)]
    return "\n".join(lines)


def _seed_truth(store: NovelStore, book: BookMeta, template: GenreTemplate | None = None) -> None:
    p = store.p
    tb = _template_blocks(template)
    if not p.constitution.exists():
        atomic.write_text_atomic(
            p.constitution,
            f"""# 《{book.title}》项目宪法

## 世界观红线（不可违反）
1. 修炼有代价：每次突破必然损耗寿元或根基，不得出现「无代价飞升」。
2. 力量有上限：主角的境界不得超过当前卷 `ceiling`，越级只能靠智取、外物或代价。
3. 死亡不可逆：已死亡角色不得无铺垫复活。
4. 地理有距离：跨区域移动需交代交通手段与耗时。

## 叙事红线
1. 第三人称限制视角，不得写出 POV 角色不可能知道的信息。
2. 主角不做违背 `boundaries` 的事，除非有完整的心理铺垫章节。
3. 每章结尾必须留钩子，不得平淡收束。

## 文风红线
1. 禁止大段排比与空洞比喻（"仿佛……又如……"）。
2. 单句平均长度 12–30 字，连续等长句不超过 4 句。
3. 对话占比 30%–50%，避免纯叙述灌水。
""",
        )
    if not p.style_fingerprint.exists():
        atomic.write_text_atomic(
            p.style_fingerprint,
            """# 文风指纹

## 句式节奏
- 短句为主，战斗段更短（平均 8–15 字/句）。
- 每 3–5 句插入一次动作或环境短句，避免连续心理独白。

## 用词偏好
- 多用实词、动词，少用形容词堆叠。
- 忌：「仿佛」「宛如」「不由得」「不由得心中一凛」「眼中闪过一丝」。

## 对话
- 短促，带信息量；忌角色互相解释设定。

## 本章避雷（反馈复盘沉淀）
（暂无。反馈复盘确认后自动追加。）
""",
        )
    if not p.world_file("power-system.md").exists():
        atomic.write_text_atomic(
            p.world_file("power-system.md"),
            """# 力量体系

## 境界阶梯
淬体（一至九重）→ 凝气 → 筑基 → 金丹 → 元婴 → 化神

## 突破代价
- 淬体每重：耗三日苦修 + 一份淬体液。
- 淬体入凝气：需「破境丹」一枚，且失败一次损伤根基。

## 禁忌
- 同境界跨阶战斗，必须有外物、地形或计谋支撑，否则视为战力崩坏。
""",
        )
    if not p.world_file("factions.md").exists():
        atomic.write_text_atomic(
            p.world_file("factions.md"),
            """# 势力与关系

| 势力 | 立场 | 与主角关系 | 备注 |
|---|---|---|---|
| 青云宗 | 正道 | 所属 | 外门弟子起点 |
| 血刀门 | 魔道 | 敌对 | 与青云宗百年血仇 |
""",
        )
    if not p.world_file("geography.md").exists():
        atomic.write_text_atomic(
            p.world_file("geography.md"),
            """# 地理

- **青云山**：青云宗山门，海拔三千丈，四季积雪。
- **青岩矿脉**：青云山西麓，产玄铁矿，宗门弟子采掘修行。
- **落霞城**：山下凡人城镇，贸易与消息集散地。
""",
        )
    if not p.world_file("rules.md").exists():
        atomic.write_text_atomic(
            p.world_file("rules.md"),
            """# 世界规则

1. 灵气浓度随海拔递减，高山修炼更快。
2. 储物袋容量固定一丈见方，不可叠加。
3. 传讯符延迟约半炷香，无法即时通信。
""",
        )
    if not p.world_state.exists():
        atomic.write_text_atomic(
            p.world_state,
            """# 当前世界状态

## 主角处境
（待第 1 章后由 Curator 更新）

## 宗门局势
（待更新）
""",
        )
    if not p.arcs.exists():
        atomic.write_text_atomic(
            p.arcs,
            """# 情感弧线与支线进度

## 主线：淬体登顶
- 起点：外门杂役
- 终点：（待定）

## 支线
（待追加）
""",
        )
    if not p.foreshadow.exists():
        store.save_foreshadow(ForeshadowBook(items=[]))
    if not p.timeline.exists():
        store.save_timeline([])
    if not p.resources.exists():
        atomic.write_json_atomic(p.resources, {"held": [], "updated_ch": 0})
    if not p.master_outline.exists():
        tpl_note = _block(tb, "opening", "golden_finger", "hook", "arc")
        atomic.write_text_atomic(
            p.master_outline,
            f"""# 《{book.title}》总纲

## 一句话故事
（待补）

## 三幕结构
1. 出山：外门逆袭，扎根青云宗。
2. 立势：卷入宗门与血刀门之争，揭开残图。
3. 破局：（待定）

## 核心爽点
- 以弱胜强的智斗
- 资源积累的可见增长
"""
            + (f"\n## 题材模板基线（来自拆书对标）\n{tpl_note}\n" if tpl_note else ""),
        )
    if not p.volume_outline(1).exists():
        beat_tpl = _block(tb, "power", "coolpoint")
        atomic.write_text_atomic(
            p.volume_outline(1),
            """# 第一卷 · 淬体

## 目标
淬体一重 → 淬体九重，站稳外门。

## 节奏
前 3 章每章设钩；每 3 章一次小爽点；每 10 章一次大冲突。
"""
            + (f"{beat_tpl}\n" if beat_tpl else ""),
        )
    if not p.threads_dir.joinpath("main.json").exists():
        store.save_thread(
            Thread(id="main", name="主线：淬体登顶", kind="main", progress="尚未开始", last_ch=0)
        )


PROFILE_PRESETS: dict[str, dict[str, Any]] = {
    "tomato": {
        "display_name": "番茄小说",
        "chapter_words": [2000, 2600],
        "hook_required": True,
        "hook_position": "last_200_words",
        "hook_min_words": 80,
        "coolpoint_interval_ch": 3,
        "sensitive_check": "strict",
        "title_max_chars": 20,
        "publish_pace": {"min_daily": 1, "note": "断更>3天流量惩罚显著"},
        "notes": "免费平台，前30章留存决定推荐池；前3章每章必设钩",
        "redline_words": ["自杀", "毒品", "赌博", "分裂国家", "枪支"],
    },
    "qidian": {
        "display_name": "起点中文网",
        "chapter_words": [2000, 4000],
        "hook_required": True,
        "hook_position": "last_200_words",
        "hook_min_words": 60,
        "coolpoint_interval_ch": 4,
        "sensitive_check": "strict",
        "title_max_chars": 20,
        "publish_pace": {"min_daily": 2, "note": "VIP 上架后断更影响订阅"},
        "notes": "付费平台，章节名≤20字，重视设定严谨度",
        "redline_words": ["自杀", "毒品", "赌博", "分裂国家", "枪支"],
    },
    "qimao": {
        "display_name": "七猫中文网",
        "chapter_words": [2000, 2500],
        "hook_required": True,
        "hook_position": "last_200_words",
        "hook_min_words": 80,
        "coolpoint_interval_ch": 2,
        "sensitive_check": "strict",
        "title_max_chars": 20,
        "publish_pace": {"min_daily": 2, "note": "免费平台，更新稳定性权重高"},
        "notes": "免费平台，节奏要求更快，爽点间隔更短",
        "redline_words": ["自杀", "毒品", "赌博", "分裂国家", "枪支"],
    },
}


def _seed_profile(store: NovelStore, platform: str, wb: WorkbenchPaths) -> None:
    preset = PROFILE_PRESETS.get(platform, PROFILE_PRESETS["tomato"])
    # 全局共享 profile 优先（符号链接友好：存在则直接复制内容作为本书副本）
    shared = wb.root / "workbench" / "profiles" / f"{platform}.json"
    if shared.exists():
        raw = atomic.read_json(shared, {}) or {}
        if not isinstance(raw, dict):
            raw = {}
        raw.setdefault("platform", platform)
        atomic.write_json_atomic(store.p.profiles / f"{platform}.json", raw)
        return
    raw = {"platform": platform, **preset}
    atomic.write_json_atomic(store.p.profiles / f"{platform}.json", raw)
    shared.parent.mkdir(parents=True, exist_ok=True)
    atomic.write_json_atomic(shared, raw)
