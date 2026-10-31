"""上下文装配引擎（§5）—— 整个工作台的核心。

六层装配 + 预算控制 + 落盘审计。任何一句话被 AI 写出，都能回答"它当时看到了什么"。

L0 宪法（硬错误）→ L1 本章契约（硬错误）→ L2 活跃伏笔 → L3 近章摘要
→ L4 检索补充 → L5 显式引用；超预算按 L4→L3→L2 裁剪，L0/L1 不可裁。
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

from ..core.domain import active_for_context, overdue_alerts
from ..core.schema import ChapterOutline, CharacterCard, ForeshadowBook
from ..core.store import NovelStore, count_cn_words

LAYER_ORDER = ["L0", "L1", "L2", "L3", "L4", "L4f", "L5"]
# 裁剪优先级：先 L4，再 L3，再 L2（§5.1）
#: 预算不够时的裁剪顺序：先砍检索旧文，再砍 few-shot 范例，最后才动摘要/伏笔
TRIM_ORDER = ["L4", "L4f", "L3", "L2"]


#: 缺陷 #81：@引用 与 style_ref 是直接拼进文件系统路径的。作者在 must_include 里写
#: `@.git/config` 或 `@.env`，就会把 git 凭据、环境变量之类本不该进上下文的内容读出来，
#: 再随 prompt 发给模型。这里统一收口：拒绝空、绝对路径、盘符、以及任何以点开头的路径段
#: （隐藏目录/隐藏文件），保留 combat/x.md、truth/xxx.md 这类正常相对引用。
def _safe_ref_token(token: str) -> str:
    t = (token or "").split("#")[0].strip().lstrip("@").strip()
    if not t:
        return ""
    norm = t.replace("\\", "/")
    if norm.startswith("/") or ":" in norm:
        return ""
    segs = [x for x in norm.split("/") if x not in ("", ".")]
    if not segs or any(x.startswith(".") for x in segs):
        return ""
    return "/".join(segs)



class AssemblyError(RuntimeError):
    """L0/L1 缺失时的硬错误：拒绝生成。"""


@dataclass
class Layer:
    key: str
    title: str
    content: str = ""
    tokens: int = 0
    source: list[str] = field(default_factory=list)
    trimmed: bool = False
    trim_note: str = ""
    mandatory: bool = False

    def empty(self) -> bool:
        return not self.content.strip()


@dataclass
class AssembledContext:
    ch: int
    layers: dict[str, Layer]
    budget: int
    system_prompt: str
    user_prompt: str
    audit_md: str
    total_tokens: int
    warnings: list[str] = field(default_factory=list)
    foreshadow_alerts: list[dict] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------- token 估算


def estimate_tokens(text: str) -> int:
    """中文 1 字 ≈ 1 token，英文 4 字符 ≈ 1 token。用于预算控制（保守）。"""
    if not text:
        return 0
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
    other = len(text) - cjk
    return cjk + (max(1, other // 4) if other else 0)


def truncate_to_tokens(text: str, max_tokens: int) -> str:
    if estimate_tokens(text) <= max_tokens:
        return text
    # 按字符近似截断，留 10% 余量
    ratio = max_tokens / max(1, estimate_tokens(text))
    cut = max(1, int(len(text) * ratio * 0.9))
    out = text[:cut]
    # 尽量在句末断开
    for sep in ("。", "！", "？", "\n"):
        idx = out.rfind(sep)
        if idx > cut * 0.6:
            return out[: idx + 1]
    return out


def compact_chars(text: str, max_tokens: int) -> str:
    return truncate_to_tokens(text, max_tokens)


# ---------------------------------------------------------------- 装配器


class ContextAssembler:
    """按 §5.1 六层装配。retrieval 为 None 时跳过 L4（降级）。"""

    def __init__(
        self,
        store: NovelStore,
        *,
        retrieval: Any | None = None,
        budget: int | None = None,
        l0_tokens: int = 1000,
        l1_tokens: int = 3000,
        l2_tokens: int = 500,
        l3_tokens: int = 3000,
        l4_tokens: int = 6000,
        recent_tail_chars: int = 500,
    ):
        self.store = store
        self.retrieval = retrieval
        self.budget = budget or 16000
        self.caps = {
            "L0": l0_tokens,
            "L1": l1_tokens,
            "L2": l2_tokens,
            "L3": l3_tokens,
            "L4": l4_tokens,
        }
        self.recent_tail_chars = recent_tail_chars

    # ---------------- 主流程 ----------------
    async def assemble(self, outline: ChapterOutline, *, book_title: str = "", genre: str = "") -> AssembledContext:
        warnings: list[str] = []
        layers: dict[str, Layer] = {}

        book = None
        try:
            book = self.store.load_book()
        except Exception as exc:
            # 缺陷 #130：load_book 挂了，静默用默认 16000，作者改的 context_budget 被悄悄忽略。
            # 必须记 warning，否则作者永远不知道为什么预算没生效。
            warnings.append(
                f"book.yaml 读取失败：{type(exc).__name__}，上下文预算已降级为默认 {self.budget}"
            )
        title = book_title or (book.title if book else "")
        budget = self.budget

        # ---- L0 ----
        l0 = self._build_l0(warnings)
        if l0.empty():
            raise AssemblyError(
                "L0 宪法为空：truth/constitution.md 与 truth/style-fingerprint.md 必须存在且非空，拒绝生成"
            )
        layers["L0"] = l0

        # ---- L1 ----
        l1 = self._build_l1(outline)
        if l1.empty():
            raise AssemblyError(f"L1 本章契约为空：第 {outline.ch} 章章纲/scenes 缺失，拒绝生成")
        layers["L1"] = l1

        # ---- L2 ----
        fb = self.store.load_foreshadow()
        layers["L2"] = self._build_l2(outline, fb)

        # ---- L3 ----
        layers["L3"] = self._build_l3(outline)

        # ---- L4 ----
        layers["L4"], l4_meta = await self._build_l4(outline, warnings)

        # ---- L4f few-shot（与 L4 共享预算）----
        layers["L4f"], l4f_meta = self._build_fewshot(outline, warnings)

        # ---- L5 ----
        layers["L5"] = self._build_l5(outline)

        # ---- 预算裁剪 ----
        total = sum(l.tokens for l in layers.values())
        if total > budget:
            total = self._trim(layers, budget, warnings)

        alerts = overdue_alerts(fb, outline.ch)
        system_prompt, user_prompt = self._render_prompts(outline, layers, title, genre or (book.genre if book else ""))
        audit = self._render_audit(
            outline, layers, title, total, budget, warnings, alerts, system_prompt, user_prompt
        )

        return AssembledContext(
            ch=outline.ch,
            layers=layers,
            budget=budget,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            audit_md=audit,
            total_tokens=total,
            warnings=warnings,
            foreshadow_alerts=alerts,
            meta={"retrieval": l4_meta, "fewshot": l4f_meta},
        )

    # ---------------- 各层 ----------------
    def _build_l0(self, warnings: list[str]) -> Layer:
        parts: list[str] = []
        sources: list[str] = []
        const = self.store.read_truth_text("constitution.md")
        style = self.store.read_truth_text("style-fingerprint.md")
        if const.strip():
            parts.append(_trim_section(const, int(self.caps["L0"] * 0.6)))
            sources.append("truth/constitution.md")
        else:
            warnings.append("L0: constitution.md 为空")
        if style.strip():
            parts.append(_trim_section(style, int(self.caps["L0"] * 0.4)))
            sources.append("truth/style-fingerprint.md")
        else:
            warnings.append("L0: style-fingerprint.md 为空，文风约束缺失")
        content = "\n\n".join(parts)
        return Layer("L0", "世界观红线 + 文风约束", content, estimate_tokens(content), sources, mandatory=True)

    def _build_l1(self, outline: ChapterOutline) -> Layer:
        # L1 是 Writer 的输入契约：没有场景、没有钩子、没有 must_include 就等于让模型自由发挥
        if not (outline.scenes or outline.hook or outline.must_include):
            return Layer("L1", "本章契约 + 出场人物", "", 0, [], mandatory=True)
        parts: list[str] = []
        sources: list[str] = [self.store.rel(self.store.p.outline_json(outline.ch))]

        if outline.scenes:
            scene_lines = []
            for i, sc in enumerate(outline.scenes, 1):
                scene_lines.append(
                    f"{i}. 地点：{sc.loc or '（未填）'}｜人物：{'、'.join(sc.chars) or '（未填）'}\n"
                    f"   目标：{sc.goal or '（未填）'}\n"
                    f"   冲突：{sc.conflict or '（未填）'}\n"
                    f"   结果：{sc.outcome or '（未填）'}"
                )
            parts.append("### 场景节拍\n" + "\n".join(scene_lines))

        parts.append(
            f"### 本章契约\n- 章号：第 {outline.ch} 章\n- 标题：{outline.title or '（未命名）'}\n"
            f"- 视角：{outline.pov}（第三人称限制视角）\n"
            f"- 推进故事线：{'、'.join(outline.threads) or '主线'}\n"
            f"- 目标字数：{outline.target_words[0]}–{outline.target_words[1]}\n"
            f"- 结尾钩子：{outline.hook or '（未指定，需自行设计但必须有）'}"
        )

        # 出场人物卡全文
        chars: list[CharacterCard] = []
        for cid in outline.char_refs():
            card = self.store.get_character(cid)
            if card is None:
                continue
            if any(c.id == card.id for c in chars):
                continue
            chars.append(card)
            sources.append(f"truth/characters/{card.id}.yaml")
        if chars:
            blocks = ["### 出场人物卡（行为/战力硬约束）"]
            for c in chars:
                blocks.append(
                    f"**{c.name}**（{c.id}，{c.role}）\n"
                    f"- 别名：{'、'.join(c.aliases) or '无'}\n"
                    f"- 性格：{'、'.join(c.personality) or '未填'}\n"
                    f"- 口癖：{'；'.join(c.speech_habits) or '未填'}\n"
                    f"- 行为边界（违反即 OOC）：{'；'.join(c.boundaries) or '未填'}\n"
                    f"- 战斗习惯/外显特征：{c.appearance or '未填'}\n"
                    f"- 战力：{c.power_scale.rank or '未填'}"
                    + (f"（本卷上限 {c.power_scale.ceiling}）" if c.power_scale.ceiling else "")
                    + (f"\n- ⚠ 禁止：{c.power_scale.forbidden}" if c.power_scale.forbidden else "")
                )
                if c.relations:
                    rels = "；".join(f"{r.type}→{r.target}" + (f"（{r.note}）" if r.note else "") for r in c.relations)
                    blocks[-1] += f"\n- 人物关系：{rels}"
            parts.append("\n".join(blocks))

        if outline.must_include:
            parts.append("### 必须包含\n" + "\n".join(f"- {m}" for m in outline.must_include))

        content = "\n\n".join(parts)
        content = truncate_to_tokens(content, self.caps["L1"])
        return Layer("L1", "本章契约 + 出场人物", content, estimate_tokens(content), sources, mandatory=True)

    def _build_l2(self, outline: ChapterOutline, fb: ForeshadowBook) -> Layer:
        items = active_for_context(fb, outline, outline.ch)
        if not items:
            return Layer("L2", "活跃伏笔", "", 0, ["truth/foreshadow.json"])
        lines = ["### 本章需要呼应的伏笔"]
        actions = {a.ref: a for a in outline.foreshadow_actions}
        for it in items:
            act = actions.get(it.id)
            head = f"- **{it.id}** {it.desc}（埋于第 {it.planted_ch} 章"
            if it.hinted_chs:
                head += f"，已提示于 {', '.join('第%d章' % h for h in it.hinted_chs)}"
            head += f"，状态 {it.status}"
            if it.deadline_ch:
                head += f"，截止第 {it.deadline_ch} 章"
            head += "）"
            lines.append(head)
            if act:
                lines.append(f"  → 本章动作：**{act.action}**" + (f"（{act.note}）" if act.note else ""))
            if it.payoff_plan:
                lines.append(f"  → 回收计划：{it.payoff_plan}")
        content = truncate_to_tokens("\n".join(lines), self.caps["L2"])
        return Layer("L2", "活跃伏笔", content, estimate_tokens(content), ["truth/foreshadow.json"])

    def _build_l3(self, outline: ChapterOutline) -> Layer:
        parts: list[str] = []
        sources: list[str] = []
        ch = outline.ch
        # 前 3 章摘要全文
        for prev in range(max(1, ch - 3), ch):
            s = self.store.read_summary(prev)
            if s.strip():
                parts.append(f"**第 {prev} 章摘要**\n{s.strip()}")
                sources.append(self.store.rel(self.store.p.summary_md(prev)))
            elif self.store.chapter_exists(prev):
                # 没有摘要时退化为取正文前 300 字
                text = self.store.read_chapter(prev)
                parts.append(f"**第 {prev} 章（无摘要，取开头）**\n{text[:300]}")
                sources.append(self.store.rel(self.store.p.chapter_md(prev)))
        # 最近 1 章尾部原文 500 字
        prev = ch - 1
        if prev >= 1 and self.store.chapter_exists(prev):
            text = self.store.read_chapter(prev)
            tail = text[-self.recent_tail_chars :] if len(text) > self.recent_tail_chars else text
            parts.append(f"**第 {prev} 章结尾原文（衔接语气与场景）**\n…{tail}")
            sources.append(self.store.rel(self.store.p.chapter_md(prev)))

        if not parts:
            return Layer("L3", "近章摘要", "", 0, [])
        content = truncate_to_tokens("\n\n".join(parts), self.caps["L3"])
        return Layer("L3", "前情摘要", content, estimate_tokens(content), sources)

    async def _build_l4(self, outline: ChapterOutline, warnings: list[str]) -> tuple[Layer, dict]:
        if self.retrieval is None:
            warnings.append("L4: 未配置检索，跳过（降级）")
            return Layer("L4", "相关旧文", "", 0, [], trim_note="retrieval disabled"), {"mode": "off"}
        try:
            available = bool(getattr(self.retrieval, "available", False))
        except Exception:
            available = False
        if not available:
            warnings.append("L4: 索引未建立，跳过（降级为无检索）")
            return Layer("L4", "相关旧文", "", 0, [], trim_note="index not ready"), {"mode": "off"}
        try:
            result = await self.retrieval.search_for_outline(outline, top_k=8, budget_chars=self.caps["L4"] * 2)
        except Exception as exc:  # 检索必须永不阻断装配
            warnings.append(f"L4: 检索失败已降级（{type(exc).__name__}）")
            return Layer("L4", "相关旧文", "", 0, [], trim_note="search failed"), {"mode": "error"}
        rendered = getattr(result, "rendered", "") or ""
        if not rendered.strip():
            return Layer("L4", "相关旧文", "", 0, [], trim_note="no hits"), {"mode": getattr(result, "mode", "bm25")}
        content = truncate_to_tokens(rendered, self.caps["L4"])
        sources = [f"[ch-{h.ch:04d}]" for h in getattr(result, "hits", [])]
        meta = {
            "mode": getattr(result, "mode", "bm25"),
            "degraded": getattr(result, "degraded", False),
            "hits": len(getattr(result, "hits", [])),
        }
        return Layer("L4", "相关旧文（带出处）", content, estimate_tokens(content), sources), meta

    # ---------------- few-shot 片段（§5.3） ----------------
    def _build_fewshot(self, outline: ChapterOutline, warnings: list[str]) -> tuple[Layer, dict]:
        """按场景类型从 ``materials/snippets/<type>/`` 取 1–2 条范例。

        §5.3：与 L4 同一预算通道、同预算上限；章纲 ``style_ref`` 可强制指定片段文件。
        写作范式示例与「相关旧文」是两件事——后者是检索召回，前者是作者
        亲手挑的文风样本，所以单独成层但要和 L4 抢同一个预算池。
        """
        snippets_dir = self.store.p.materials / "snippets"
        picked: list[tuple[str, str]] = []  # (来源, 正文)
        kinds = self._scene_kinds(outline)

        # 1) style_ref 强制指定（优先，且可指定目录外的文件）
        ref = (outline.style_ref or "").strip()
        if ref:
            path = self._resolve_snippet_ref(ref, snippets_dir)
            if path is None:
                warnings.append(f"L4-fewshot: style_ref「{ref}」未找到，已忽略")
            else:
                try:
                    picked.append((self.store.rel(path), path.read_text(encoding="utf-8").strip()))
                except Exception:
                    warnings.append(f"L4-fewshot: style_ref「{ref}」读取失败，已忽略")

        # 2) 按场景类型补齐到 2 条
        budget_note = ""
        if len(picked) < 2 and snippets_dir.exists():
            for kind in kinds:
                if len(picked) >= 2:
                    break
                group = sorted((snippets_dir / kind).glob("*.md")) if (snippets_dir / kind).exists() else []
                for f in group:
                    if len(picked) >= 2:
                        break
                    if any(self.store.rel(f) == src for src, _ in picked):
                        continue
                    try:
                        body = f.read_text(encoding="utf-8").strip()
                    except Exception:
                        continue
                    if body:
                        picked.append((self.store.rel(f), body))

        if not picked:
            return Layer("L4f", "范例片段", "", 0, [], trim_note="no snippets"), {"picked": 0}

        # 与 L4 共享预算：片段最多占 L4 预算的 1/3，保证「旧文检索」不被挤没
        cap = max(200, self.caps["L4"] // 3)
        parts: list[str] = []
        sources: list[str] = []
        for src, body in picked:
            block = f"### 范例（{src}）\n{body[:800]}"
            if estimate_tokens("\n\n".join([*parts, block])) > cap and parts:
                budget_note = f"（超出片段预算，仅注入 {len(sources)} 条）"
                break
            parts.append(block)
            sources.append(src)
        content = "\n\n".join(parts)
        content = truncate_to_tokens(content, cap)
        return (
            Layer(
                "L4f",
                "范例片段（文风参照，不可照抄情节）",
                content,
                estimate_tokens(content),
                sources,
                trim_note=budget_note,
            ),
            {"picked": len(sources), "kinds": kinds, "cap_tokens": cap},
        )

    @staticmethod
    def _scene_kinds(outline: ChapterOutline) -> list[str]:
        """从章纲推断场景类型（combat/emotion/scene/dialogue）。"""
        blob = " ".join(
            [
                outline.title or "",
                outline.beat or "",
                outline.hook or "",
                *[f"{s.goal} {s.conflict} {s.outcome}" for s in outline.scenes],
            ]
        )
        kinds: list[str] = []
        rules = {
            "combat": ("战", "打", "斗", "剑", "刀", "拳", "杀", "冲突", "交手", "追击"),
            "emotion": ("情", "泪", "痛", "恨", "心动", "别", "念", "悔", "怒", "哀"),
            "dialogue": ("谈", "问", "答", "议", "说", "劝", "争", "谈判", "质问"),
            "scene": ("景", "山", "城", "夜", "雨", "雪", "路", "门前", "渡口", "矿"),
        }
        for kind, kws in rules.items():
            if any(k in blob for k in kws):
                kinds.append(kind)
        if not kinds:
            kinds.append("scene")
        return kinds

    def _resolve_snippet_ref(self, ref: str, snippets_dir):
        """解析 style_ref：支持 ``combat/x.md``、``snippets/combat/x.md``、``@路径``。"""
        token = _safe_ref_token(ref)
        if not token:
            return None
        candidates = [
            snippets_dir / token,
            self.store.p.materials / token,
            self.store.p.root / token,
        ]
        if not token.endswith(".md"):
            candidates.insert(0, snippets_dir / f"{token}.md")
        for c in candidates:
            try:
                if c.exists() and c.is_file() and self.store.p.root.resolve() in c.resolve().parents:
                    return c
            except OSError:
                continue
        return None

    def _build_l5(self, outline: ChapterOutline) -> Layer:
        refs: list[tuple[str, str]] = []
        for m in outline.must_include:
            for token in re.findall(r"@([^\s，,。；;]+)", m):
                refs.append((token, m))
        if not refs:
            return Layer("L5", "显式引用", "", 0, [])
        parts: list[str] = []
        sources: list[str] = []
        seen: set[str] = set()
        for token, ctx in refs:
            path = self._resolve_ref(token)
            if path is None:
                parts.append(f"- `@{token}`（未找到，引用自：{ctx}）")
                continue
            if path.as_posix() in seen:
                continue
            seen.add(path.as_posix())
            try:
                body = path.read_text(encoding="utf-8")
            except Exception:
                continue
            sources.append(self.store.rel(path))
            parts.append(f"### @{token}（{self.store.rel(path)}）\n{body.strip()[:1500]}")
        if not parts:
            return Layer("L5", "显式引用", "", 0, [])
        content = "\n\n".join(parts)
        return Layer("L5", "显式引用卡片", content, estimate_tokens(content), sources)

    def _resolve_ref(self, token: str):
        """解析 @路径：支持 truth/ 省略、characters/ 前缀、#分节、无扩展名。"""
        token = _safe_ref_token(token)
        if not token:
            return None
        bare = token[len("characters/"):] if token.startswith("characters/") else token
        candidates = [
            self.store.p.root / token,
            self.store.p.truth / token,
            self.store.p.world_dir / token,
            self.store.p.characters_dir / f"{bare}.yaml",
            self.store.p.characters_dir / f"{bare}.yml",
            self.store.p.materials / token,
            self.store.p.outline / token,
        ]
        if not token.endswith((".md", ".yaml", ".yml", ".json")):
            candidates[:0] = [
                self.store.p.truth / f"{token}.md",
                self.store.p.characters_dir / f"{bare}.yaml",
                self.store.p.world_dir / f"{bare}.md",
            ]
        for c in candidates:
            try:
                if c.exists() and c.is_file() and self.store.p.root.resolve() in c.resolve().parents:
                    return c
            except OSError:
                continue
        return None

    # ---------------- 裁剪 ----------------
    def _trim(self, layers: dict[str, Layer], budget: int, warnings: list[str]) -> int:
        total = sum(l.tokens for l in layers.values())
        for key in TRIM_ORDER:
            if total <= budget:
                break
            layer = layers.get(key)
            if layer is None or layer.empty():
                continue
            need = total - budget
            keep = max(0, layer.tokens - need)
            if keep <= 0:
                layer.content = ""
                layer.tokens = 0
                layer.trimmed = True
                layer.trim_note = "已整层裁掉（超预算）"
            else:
                layer.content = truncate_to_tokens(layer.content, keep)
                layer.tokens = estimate_tokens(layer.content)
                layer.trimmed = True
                layer.trim_note = f"截断至 {layer.tokens} tokens（预算 {budget}）"
            warnings.append(f"预算超限：{key} 被裁剪（{layer.trim_note}）")
            total = sum(l.tokens for l in layers.values())
        if total > budget:
            warnings.append(f"裁剪后仍超预算：{total} > {budget}（L0/L1 不可裁）")
        return total

    # ---------------- 渲染 ----------------
    def _render_prompts(
        self, outline: ChapterOutline, layers: dict[str, Layer], title: str, genre: str
    ) -> tuple[str, str]:
        l0, l1 = layers["L0"], layers["L1"]
        system = f"""你是《{title}》的执笔者。这是一部{genre or '长篇'}网文。严格遵守以下全部约束：

## 世界观红线（L0）
{l0.content}

## 写作要求
- 字数 {outline.target_words[0]}–{outline.target_words[1]}（按中文字符计）
- 视角：{outline.pov} 第三人称限制视角，不得写出该角色不可能知道的信息
- 必须包含：{'；'.join(outline.must_include) if outline.must_include else '（无硬性要求）'}
- 结尾钩子：{outline.hook or '自行设计一个强钩子'}
- 禁止：照抄"相关旧文"原句；使用与人物卡/世界观冲突的旧设定；角色做出违反行为边界的事
- 若有"范例片段"：只学它的句式、节奏、对话密度与叙事距离，**不得复用其人物、地名、情节**
- 只输出小说正文（Markdown 段落），不要输出解释、大纲、标题以外的前后缀说明"""

        blocks: list[str] = []
        for key in ("L1", "L2", "L3", "L4", "L4f", "L5"):
            layer = layers.get(key)
            if layer and not layer.empty():
                blocks.append(f"## {layer.title}（{key}）\n{layer.content}")
        user = "\n\n".join(blocks) + f"\n\n请开始写第 {outline.ch} 章。"
        return system, user

    def _render_audit(
        self,
        outline: ChapterOutline,
        layers: dict[str, Layer],
        title: str,
        total: int,
        budget: int,
        warnings: list[str],
        alerts: list[dict],
        system_prompt: str = "",
        user_prompt: str = "",
    ) -> str:
        lines = [
            f"# 第 {outline.ch} 章 装配审计",
            "",
            f"- 书名：{title}",
            f"- 总 tokens：{total} / 预算 {budget}（{'超限' if total > budget else '合规'}）",
            f"- 生成时间：{_now()}",
            "",
            "## 分层用量",
            "",
            "| 层 | 内容 | tokens | 状态 | 来源 |",
            "|---|---|---|---|---|",
        ]
        for key in LAYER_ORDER:
            layer = layers.get(key)
            if layer is None:
                continue
            status = "已裁剪" if layer.trimmed else ("空" if layer.empty() else "ok")
            src = ", ".join(layer.source[:4]) or "-"
            lines.append(f"| {key} | {layer.title} | {layer.tokens} | {status} | {src} |")

        if warnings:
            lines += ["", "## 告警", ""] + [f"- {w}" for w in warnings]
        if alerts:
            lines += ["", "## 伏笔报警", ""] + [
                f"- [{a['level']}] {a['id']} {a['desc']}：{a.get('suggestion','')}" for a in alerts
            ]
        lines += [
            "",
            "## 实际发送的 System Prompt",
            "",
            "```",
            system_prompt,
            "```",
            "",
            "## 实际发送的 User Prompt",
            "",
            "```",
            user_prompt,
            "```",
            "",
            "## 完整装配内容（可溯源）",
            "",
        ]
        for key in LAYER_ORDER:
            layer = layers.get(key)
            if layer and not layer.empty():
                lines += [f"### {key} · {layer.title}", "", "```", layer.content, "```", ""]
        return "\n".join(lines)


def _trim_section(text: str, max_tokens: int) -> str:
    """宪法/文风按优先级截取：保留标题结构，超长按 token 截断。"""
    if estimate_tokens(text) <= max_tokens:
        return text.strip()
    return truncate_to_tokens(text.strip(), max_tokens)


def _now() -> str:
    import datetime as dt

    return dt.datetime.now().isoformat(timespec="seconds")


def save_assembly(store: NovelStore, ctx: AssembledContext) -> str:
    """落盘审计（§5.1：装配结果完整落盘到 memory/ch-XXXX.assembled.md）。"""
    store.write_assembled(ctx.ch, ctx.audit_md)
    return store.rel(store.p.assembled_md(ctx.ch))
