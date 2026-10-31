
"""Curator（§6.4）：章 final 后产出提案包（不直接写盘）。

提案 → UI diff 确认 → apply。这是 truth/ 的唯一写入口。
"""
from __future__ import annotations

import datetime as dt
import json
import posixpath
from dataclasses import dataclass, field
from typing import Any

from ..core import atomic
from ..core.domain import (
    apply_foreshadow_action,
    normalize_timeline,
    validate_timeline,
)
from ..core.schema import (
    ChapterOutline,
    EntityDelta,
    ForeshadowBook,
    Proposal,
    ProposalPack,
    TimelineEntry,
)
from ..core.store import PROPOSAL_FILE_PREFIXES, NovelStore
from ..model.gateway import ModelGateway
from .prompts import CURATOR_SYSTEM, curator_prompt

# 允许 Curator 触碰的文件白名单（§6.4 提案目标）。
# 真身在 core：删除面白名单是硬约束，不该让 core 反过来 import agents（架构铁律 #54）。
ALLOWED_PREFIXES = PROPOSAL_FILE_PREFIXES

ALLOWED_OPS = {"create", "patch", "append", "replace"}

#: §9.3：category → 避雷建议文案（沉淀进 style-fingerprint.md）
AVOID_ADVICE = {
    "ai-flavor": "替换套语（仿佛/宛如/眼中闪过一丝），并检查句长是否等长",
    "repetition": "同一冲突结构不得连续两章复用；写完通读相邻章比对",
    "OOC": "对照角色卡 boundaries，越界行为必须有心理铺垫",
    "power": "不得突破 power_scale.forbidden，越阶靠外物/地形/计谋",
    "redline": "命中平台红线词，导出会拦截",
    "timeline": "日期与章号必须单调一致",
    "pov": "第三人称限制视角，不写视角角色不可能知道的信息",
    "hook": "章末必须留钩子，平收不算",
    "words": "对准平台字数区间，写完核对实际字数",
}


@dataclass
class CurateResult:
    ch: int
    pack: ProposalPack
    previews: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


class Curator:
    def __init__(self, store: NovelStore, gateway: ModelGateway):
        self.store = store
        self.gw = gateway

    async def curate(self, ch: int, *, use_model: bool = True, save_pack: bool = True) -> CurateResult:
        text = self.store.read_chapter(ch)
        if not text.strip():
            raise ValueError(f"第 {ch} 章正文为空，无法沉淀")
        outline = self.store.load_outline(ch) or ChapterOutline(ch=ch)
        warnings: list[str] = []
        proposals: list[Proposal] = []

        if use_model:
            fb = self.store.load_foreshadow()
            prompt = curator_prompt(
                ch=ch,
                title=outline.title,
                text=text,
                outline=outline,
                foreshadow_brief="\n".join(
                    f"- {i.id}：{i.desc}（{i.status}，埋 {i.planted_ch}，截止 {i.deadline_ch}）" for i in fb.items
                ),
                timeline_brief="\n".join(
                    f"- 第{e.ch}章 {e.date}：{e.event}" for e in self.store.load_timeline()[-8:]
                ),
                world_state=self.store.read_truth_text("world-state.md"),
                characters_brief="\n".join(
                    f"- {c.id}（{c.name}）{c.power_scale.rank}" for c in self.store.list_characters()
                ),
            )
            try:
                payload = await self.gw.complete_json("curator", prompt, system=CURATOR_SYSTEM, chapter=ch)
                proposals = self._to_proposals(payload, ch, warnings)
            except Exception as exc:
                warnings.append(f"Curator 模型产出失败（{type(exc).__name__}），已退化为规则沉淀")

        # 规则兜底：至少产出章摘要 + 伏笔动作 + 时间线条目 + deltas
        if not proposals:
            proposals = self._rule_proposals(ch, text, outline, warnings)
        else:
            # 章纲里的伏笔/时间线是「作者已确认的事实」，不依赖模型自由发挥：
            # 模型产出里缺哪类就补哪类，避免伏笔池永远空着（实测出现过）。
            proposals = self._ensure_structural(ch, text, outline, proposals, warnings)

        proposals = self._dedupe(proposals)
        packs = ProposalPack(ch=ch, proposals=proposals, model="curator", created=_now())

        previews: dict[str, str] = {}
        for p in proposals:
            if not p.id:
                p.id = f"p-{ch}-{len(previews) + 1}"
            try:
                previews[p.id] = self.store.preview_proposal(p)
            except Exception as exc:
                p.conflict = f"预览失败: {exc}"
                previews[p.id] = f"[预览失败] {exc}"

        if save_pack:
            self._save_pack(packs)
        return CurateResult(ch=ch, pack=packs, previews=previews, warnings=warnings)

    # ---------------- 提案校验 ----------------
    def _to_proposals(self, payload: dict[str, Any], ch: int, warnings: list[str]) -> list[Proposal]:
        raw = payload.get("proposals") or []
        out: list[Proposal] = []
        for i, item in enumerate(raw, 1):
            if not isinstance(item, dict):
                continue
            f = str(item.get("file", "")).strip().lstrip("/")
            op = str(item.get("op", "patch"))
            if not f:
                warnings.append(f"提案 {i} 缺 file 字段，已丢弃")
                continue
            if not _allowed(f):
                warnings.append(f"提案 {i} 目标 {f} 不在 Curator 可写白名单，已丢弃")
                continue
            if op not in ALLOWED_OPS:
                warnings.append(f"提案 {i} 的 op={op} 不合法，已改为 patch")
                op = "patch"
            # 章号一致性：防止模型把别章摘要写到本章
            if "summary" in f and f"ch-{ch:04d}" not in f:
                warnings.append(f"提案 {i} 摘要文件名与章号不符，已纠正为 ch-{ch:04d}")
                f = f"memory/ch-{ch:04d}.summary.md"
            prop = Proposal(
                id=str(item.get("id") or f"p-{ch}-{i}"),
                file=f,
                op=op,  # type: ignore[arg-type]
                content=item.get("content"),
                patch=item.get("patch") if isinstance(item.get("patch"), dict) else None,
                section=item.get("section"),
                lines=[str(x) for x in item.get("lines", [])] if isinstance(item.get("lines"), list) else None,
                reason=str(item.get("reason", ""))[:300],
            )
            if prop.op == "create" and not (prop.content or "").strip():
                warnings.append(f"提案 {i} 是 create 但内容为空，已丢弃")
                continue
            if prop.file.endswith(".jsonl") and prop.op == "append":
                prop.lines = [l for l in (prop.lines or []) if _valid_jsonl(l, ch)]
            out.append(prop)
        return out

    # ---------------- 规则兜底 ----------------
    def _rule_proposals(
        self, ch: int, text: str, outline: ChapterOutline, warnings: list[str]
    ) -> list[Proposal]:
        out: list[Proposal] = []
        # 1. 章摘要
        summary = self._summarize_rule(text, outline)
        out.append(
            Proposal(
                id=f"p-{ch}-1",
                file=f"memory/ch-{ch:04d}.summary.md",
                op="create",
                content=summary,
                reason="规则沉淀：章摘要",
            )
        )
        # 2. 伏笔动作（按章纲）
        for act in outline.foreshadow_actions:
            patch: dict[str, Any] = {}
            if act.action == "hint":
                fb = self.store.load_foreshadow()
                item = fb.get(act.ref)
                hinted = list(item.hinted_chs) if item else []
                if ch not in hinted:
                    hinted.append(ch)
                patch = {act.ref: {"status": "hinted", "hinted_chs": sorted(hinted)}}
            elif act.action == "resolve":
                patch = {act.ref: {"status": "resolved", "resolution_ch": ch}}
            elif act.action == "drop":
                patch = {act.ref: {"status": "dropped", "dropped_reason": act.note or "（未说明）"}}
            elif act.action == "plant":
                patch = {
                    act.ref: {
                        "desc": act.note or "（待补充）",
                        "planted_ch": ch,
                        "status": "planted",
                    }
                }
            if patch:
                out.append(
                    Proposal(
                        id=f"p-{ch}-f{len(out)}",
                        file="truth/foreshadow.json",
                        op="patch",
                        patch=patch,
                        reason=f"章纲伏笔动作 {act.ref}:{act.action}",
                    )
                )
        # 3. 时间线条目（若章纲/正文有日期线索）
        date = _extract_date(text)
        if date:
            out.append(
                Proposal(
                    id=f"p-{ch}-t",
                    file="truth/timeline.yaml",
                    op="append",
                    patch={"ch": ch, "date": date, "event": outline.title or f"第{ch}章事件"},
                    reason="时间线追加",
                )
            )
        # 4. entity-deltas
        delta = EntityDelta(ch=ch, entity=outline.pov or "mc", kind="state", op="update", note=f"第{ch}章结束")
        out.append(
            Proposal(
                id=f"p-{ch}-d",
                file="memory/entity-deltas.jsonl",
                op="append",
                lines=[json.dumps(delta.model_dump(mode="json"), ensure_ascii=False)],
                reason="实体变化流",
            )
        )
        return out

    def _ensure_structural(
        self,
        ch: int,
        text: str,
        outline: ChapterOutline,
        proposals: list[Proposal],
        warnings: list[str],
    ) -> list[Proposal]:
        """补齐模型容易漏掉的「结构性提案」：伏笔动作、时间线。

        章纲里的 ``foreshadow_actions`` 是作者确认过的事实，模型漏了就由规则补上，
        否则伏笔状态机在真实使用中是空的（这是实测发现的行为缺陷，不只是 demo 问题）。
        """
        has_fs = any(p.file == "truth/foreshadow.json" for p in proposals)
        has_tl = any(p.file == "truth/timeline.yaml" for p in proposals)

        if not has_fs and outline.foreshadow_actions:
            rule = [p for p in self._rule_proposals(ch, text, outline, warnings) if p.file == "truth/foreshadow.json"]
            if rule:
                proposals.extend(rule)
                warnings.append(f"模型未产出伏笔提案，已按章纲补 {len(rule)} 条")

        if not has_tl:
            date = _extract_date(text)
            if date:
                proposals.append(
                    Proposal(
                        id=f"p-{ch}-tl",
                        file="truth/timeline.yaml",
                        op="append",
                        patch={"ch": ch, "date": date, "event": outline.title or f"第{ch}章事件"},
                        reason="时间线追加（规则补）",
                    )
                )
        return proposals

    def _summarize_rule(self, text: str, outline: ChapterOutline) -> str:
        body = text.strip()
        # 取正文首段与末段拼一个事实摘要（不用模型也让摘要可读）
        paras = [p.strip() for p in body.split("\n\n") if p.strip() and not p.strip().startswith("#")]
        head = paras[0][:160] if paras else ""
        tail = paras[-1][:160] if len(paras) > 1 else ""
        scenes = "、".join(s.outcome or s.goal for s in outline.scenes if s.goal or s.outcome)
        return (
            f"# 第{outline.ch}章 摘要\n\n"
            f"**标题**：{outline.title or '（未命名）'}\n\n"
            f"**事件**：{scenes or '（章纲未填场景）'}\n\n"
            f"**开头**：{head}\n\n"
            f"**结尾**：{tail}\n\n"
            f"**钩子**：{outline.hook or '（无）'}\n"
        )

    # ---------------- 落盘 ----------------
    def _save_pack(self, pack: ProposalPack) -> None:
        path = self.store.p.root / "memory" / f"proposals-{pack.ch:04d}.json"
        from ..core import atomic

        atomic.write_json_atomic(path, pack.model_dump(mode="json"))

    def load_pack(self, ch: int) -> ProposalPack | None:
        path = self.store.p.root / "memory" / f"proposals-{ch:04d}.json"
        from ..core import atomic

        raw = atomic.read_json(path)
        if raw is None:
            return None
        # 缺陷 #98：提案包被外部写成标量/列表时 pydantic 抛 ValidationError，
        # 会顶着英文 model_type 报错冒到确认链路。读不出就当没有提案包。
        if not isinstance(raw, dict):
            return None
        return ProposalPack.model_validate(raw)

    def _dedupe(self, proposals: list[Proposal]) -> list[Proposal]:
        """同一文件同一 op 的 patch 合并，避免重复提案互相覆盖。"""
        merged: list[Proposal] = []
        index: dict[tuple[str, str], int] = {}
        for p in proposals:
            key = (p.file, "patch" if p.op == "patch" else p.op)
            if p.op == "patch" and key in index:
                base = merged[index[key]]
                base.patch = _merge_patch(base.patch or {}, p.patch or {})
                base.reason = (base.reason + "；" + p.reason)[:300]
                continue
            index[key] = len(merged)
            merged.append(p)
        return merged

    # ---------------- 确认落盘 ----------------
    def confirm(
        self,
        ch: int,
        accepted_ids: list[str] | None = None,
        *,
        commit: bool = True,
    ) -> dict[str, Any]:
        """作者确认提案 → 落盘（§4.3 纪律 2 的唯一写入口）。"""
        pack = self.load_pack(ch)
        if pack is None:
            raise ValueError(f"第 {ch} 章没有待确认的提案包，请先 POST /api/curate")

        warnings: list[str] = []
        applied: list[dict[str, Any]] = []
        diffs: dict[str, str] = {}
        # 缺陷 #114：一条都没落下去也算成功（confirm 只把失败塞进 warnings），
        # 调用方（CLI / API）无法从返回值判断——和 #113 是同一类「告警没走到出口」。
        failed = 0
        for p in pack.proposals:
            if accepted_ids is not None and p.id not in accepted_ids:
                p.accepted = False
                continue
            try:
                diff = self.store.apply_proposal(p)
                p.accepted = True
                applied.append({"id": p.id, "file": p.file, "op": p.op})
                diffs[p.id] = diff
            except Exception as exc:
                p.accepted = False
                failed += 1  # 缺陷 #114
                warnings.append(f"{p.id} ({p.file}) 落盘失败: {exc}")

        # 校验提示（不阻断）
        tl_issues = validate_timeline(self.store.load_timeline())
        for issue in tl_issues:
            warnings.append(f"时间线[{issue.level}] {issue.message}")

        # 提案包标记为已处理
        from ..core import atomic

        atomic.write_json_atomic(
            self.store.p.root / "memory" / f"proposals-{ch:04d}.json", pack.model_dump(mode="json")
        )

        # git：truth 变更单独 commit
        commit_result = None
        if commit:
            from ..core.gitbot import GitBot

            gb = GitBot(self.store.root)
            files = sorted({a["file"] for a in applied})
            commit_result = gb.commit_paths(files + [f"memory/proposals-{ch:04d}.json"], f"truth: ch-{ch:04d} 沉淀确认")

        # 状态推进：drafted/reviewed → final
        outline = self.store.load_outline(ch)
        became_final = False
        if outline and outline.status in ("reviewed", "drafted"):
            outline.status = "final"  # type: ignore[assignment]
            self.store.save_outline(outline)
            became_final = True

        # §8.5：章 final 后增量更新检索索引（失败降级，绝不阻断沉淀落盘）
        index_report = None
        if became_final:
            index_report = self._update_index(ch, warnings)

        # §9.3：把审稿里反复出现的 category 沉淀为文风指纹避雷条目（阈值触发）
        avoid_entries: list[str] = []
        if became_final:
            avoid_entries = self._distill_avoid_entries(ch, warnings)

        return {
            "ch": ch,
            "applied": applied,
            "failed": failed,  # 缺陷 #114：落盘失败条数，调用方据此决定退出码
            "skipped": [p.id for p in pack.proposals if p.accepted is False],
            "warnings": warnings,
            "diffs": diffs,
            "commit": commit_result.__dict__ if commit_result else None,
            "became_final": became_final,
            "index_update": index_report,
            "avoid_entries": avoid_entries,
        }

    # ---------------- §8.5 索引增量 ----------------
    def _update_index(self, ch: int, warnings: list[str]) -> dict[str, Any]:
        """把刚定稿的章增量写进 FTS5 索引。

        索引是**可重建缓存**（§8.5 明确 `.rag-index/` 不入 git），所以这里任何失败
        都只记 warning——索引坏了可以重建，沉淀数据丢了不可恢复。
        """
        try:
            from ..retrieval.index import IndexBuilder

            builder = IndexBuilder(self.store)
            if not builder.index_ready():
                warnings.append("索引未建立，跳过增量更新（可 POST /api/books/{b}/index/build 全量建）")
                return {"updated": False, "reason": "index_not_ready"}
            n = builder.add_chapter(ch)
            return {"updated": True, "chunks": n}
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"索引增量更新失败（{type(exc).__name__}: {exc}），可后续全量重建")
            return {"updated": False, "reason": f"{type(exc).__name__}"}

    # ---------------- §9.3 避雷条目沉淀 ----------------
    #: 避雷条目的沉淀门槛（三个条件同时满足）：
    #:   1. 同一 category 在窗口内命中的章数 ≥ MIN_HITS；
    #:   2. 命中章数 ÷ 窗口内已审章数 ≥ MIN_RATIO（防小样本误判）；
    #:   3. **只统计 high 级问题**——避雷条目写进宪法会影响后续每一章的生成，
    #:      只有 high 级问题值得上升到这个层级；mid/low 留给 Reviewer 每章提示即可。
    #: 早先用「任意 severity + MIN_HITS=3」，结果 mock 正文的 mid 级模板重复
    #: 立刻被写成「反复出现：repetition」，等于用误报污染真相源（实测过）。
    AVOID_WINDOW = 20
    AVOID_MIN_HITS = 5
    AVOID_MIN_RATIO = 0.4
    AVOID_SEVERITIES = ("high",)

    def _distill_avoid_entries(self, ch: int, warnings: list[str]) -> list[str]:
        """统计最近若干章的审稿 category，反复出现的写进文风指纹。

        §9.3 原文：「哪些 category 的问题反复出现 → 沉淀为 style-fingerprint 避雷条目」。
        这里做成**阈值触发**：单章偶发不沉淀，避免文风指纹被一次误报污染。
        """
        try:
            counter: dict[str, list[int]] = {}
            for n in range(max(1, ch - self.AVOID_WINDOW + 1), ch + 1):
                path = self.store.p.review_json(n)
                if not path.exists():
                    continue
                try:
                    import json as _json

                    data = _json.loads(path.read_text(encoding="utf-8"))
                except Exception:
                    continue
                seen: set[str] = set()
                for issue in data.get("issues", []):
                    if str(issue.get("severity", "")) not in self.AVOID_SEVERITIES:
                        continue
                    cat = str(issue.get("category", "")).strip()
                    if cat and cat not in seen:
                        seen.add(cat)
                        counter.setdefault(cat, []).append(n)

            reviewed = sum(
                1 for n in range(max(1, ch - self.AVOID_WINDOW + 1), ch + 1)
                if self.store.p.review_json(n).exists()
            )
            hits = {
                c: chs
                for c, chs in counter.items()
                if len(chs) >= self.AVOID_MIN_HITS
                and reviewed > 0
                and len(chs) / reviewed >= self.AVOID_MIN_RATIO
            }
            if not hits:
                return []

            existing = self.store.read_truth_text("style-fingerprint.md")
            new_lines: list[str] = []
            for cat, chs in sorted(hits.items(), key=lambda kv: -len(kv[1])):
                marker = f"反复出现：{cat}"
                if marker in existing:
                    continue
                new_lines.append(
                    f"- 「{marker}」（最近 {len(chs)} 章命中：{', '.join(f'ch-{c:04d}' for c in chs[-6:])}）"
                    f"——{AVOID_ADVICE.get(cat, '写前自查该类别，避免同类问题重复出现')}"
                )
            if not new_lines:
                return []

            section = "## 反复出现的问题（自动沉淀）"
            body = "\n".join(new_lines)
            if section in existing:
                updated = existing.replace(section, f"{section}\n{body}", 1)
            else:
                updated = existing.rstrip() + f"\n\n{section}\n{body}\n"
            atomic.write_text_atomic(self.store.p.style_fingerprint, updated)

            return new_lines
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"避雷条目沉淀失败（{type(exc).__name__}: {exc}）")
            return []


# ---------------------------------------------------------------- 工具


def _allowed(path: str) -> bool:
    """提案目标白名单校验（§6.4）。

    必须先规范化再比对：直接 startswith 会让 `memory/../truth/x.md` 这类
    带 `..` 的路径蒙混过关——白名单说「只能写 memory/」，实际却落到别处，
    即「白名单逃逸」。存储层的 resolve() 虽然能挡住逃出书根，但挡不住
    逃出白名单目录，两层得各自守住。
    """
    norm = posixpath.normpath(path.strip().lstrip("/").replace("\\", "/"))
    if norm in (".", "..") or norm.startswith("../"):
        return False
    return any(norm == p or norm.startswith(p) for p in ALLOWED_PREFIXES)


def _valid_jsonl(line: str, ch: int) -> bool:
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return False
    if not isinstance(obj, dict):
        return False
    obj.setdefault("ch", ch)
    return True


def _merge_patch(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """两段 patch 合并：同 id 的字段合并，不同 id 共存。"""
    out = dict(a)
    for k, v in b.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = {**out[k], **v}
        else:
            out[k] = v
    return out


_DATE_RE = None


#: 纪年名常见长度极短（青云历/玄历/天元历），相邻汉字又常是动词（「刻着青云历」），
#: 所以正则宽取 1–6 字再由 _trim_era 收敛，避免把动词吞进纪年名。
_DATE_RE = None


#: 常见的「动词/介词」结尾字——纪年名不会以它们收尾，削掉
_ERA_STOPWORDS = ("着", "了", "在", "的", "于", "自", "从", "到", "至", "得", "记", "写", "刻")


def _trim_era(era: str) -> str:
    """把「刻着青云／他记得玄」这类粘连前缀削成真正的纪年名。

    纪年名在网文里几乎都是「1–2 字 + 历」（玄历、青云历、天元历）：
    先取末尾 2 字；若首字是动词/介词（着/得/记…），说明只粘了 1 个字，取末尾 1 字。
    """
    era = era.strip()
    if len(era) >= 2:
        tail2 = era[-2:]
        if tail2[0] in _ERA_STOPWORDS:
            return tail2[1]
        return tail2
    return era


def _extract_date(text: str) -> str:
    import re

    global _DATE_RE
    if _DATE_RE is None:
        _DATE_RE = re.compile(
            r"([一-鿿]{1,6})历\s*(\d{1,4})\s*年\s*"
            r"([\d一二三四五六七八九十正冬腊廿卅]{1,3})\s*月\s*"
            r"([初]?[\d一二三四五六七八九十廿卅]{1,3})\s*日?"
        )
    m = _DATE_RE.search(text)
    if not m:
        return ""
    era = _trim_era(m.group(1))
    return f"{era}历{m.group(2)}年{m.group(3)}月{m.group(4)}"


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")
