"""Agent 公共部分：提示词模板 + 结构化输出的健壮解析（§17.B 模板索引）。

模板集中在这里，便于调参与审计；agent 只负责组装输入、校验输出。
"""
from __future__ import annotations

import json
import re
from typing import Any

from ..core.schema import ChapterOutline, CharacterCard, PlatformProfile, ReviewIssue

# ================================================================ Planner

PLANNER_SYSTEM = """你是资深网文策划。任务是为长篇连载网文起草**章纲草案**，不是正文。

必须遵守：
1. 每章必须有：hook（结尾钩子）、pov、threads（推进哪条线）
2. 伏笔动作只能引用「已有伏笔清单」里的 id；确需新增时用 action="plant" 并在 note 写清描述
3. power_events 不得违反任何角色的 power_scale.forbidden
4. 每章至少一个场景，场景里写清 goal/conflict/outcome（冲突-奖励-期待结构）
5. 只输出 JSON，不要解释、不要 Markdown 围栏

输出格式：
{"chapters":[{"ch":int,"title":str,"pov":str,"threads":[str],"scenes":[{"loc":str,"chars":[str],"goal":str,"conflict":str,"outcome":str}],"must_include":[str],"foreshadow_actions":[{"ref":str,"action":"plant|hint|resolve|drop","note":str}],"power_events":[{"char":str,"field":"rank","to":str,"note":str}],"hook":str,"target_words":[int,int],"beat":str}]}"""


def planner_prompt(
    *,
    title: str,
    genre: str,
    start_ch: int,
    count: int,
    master: str,
    volume: str,
    threads: list[dict[str, Any]],
    world_state: str,
    character_brief: str,
    foreshadow_brief: str,
    overdue_brief: str,
    pacing_brief: str,
    style_brief: str,
    target_words: tuple[int, int],
) -> str:
    return f"""# 任务
为《{title}》（{genre}）生成 **{count} 章**章纲草案，从第 {start_ch} 章开始（即第 {start_ch}–{start_ch + count - 1} 章）。
每章目标字数 {target_words[0]}–{target_words[1]}。

# 总纲
{master[:3000]}

# 当前卷纲
{volume[:2000]}

# 故事线进度
{threads_block(threads)}

# 当前世界状态
{world_state[:1500]}

# 人物速览
{character_brief[:2000]}

# 已有伏笔清单（只能引用这些 id）
{foreshadow_brief or '（暂无）'}

# 伏笔报警（必须在近期处理）
{overdue_brief or '（无）'}

# 节奏基线（来自拆书对标）
{pacing_brief or '（未导入对标书，按题材常规节奏）'}

# 文风与避雷
{style_brief[:1200]}

# 硬性要求
- 章节号连续递增，不要跳号
- 每章 hook 必须具体（写出画面或未解信息），禁止"且听下回分解"这类空钩
- 前 3 章每章都必须有钩子（免费平台留存关键）
- 伏笔动作要克制：一批 {count} 章里新增伏笔不超过 2 个
- must_include 里引用设定用 @路径 语法，如 "@characters/elder-wang 首次登场"

只输出 JSON。"""


def threads_block(threads: list[dict[str, Any]]) -> str:
    if not threads:
        return "（暂无故事线记录）"
    lines = []
    for t in threads:
        lines.append(f"- {t.get('id')}「{t.get('name')}」（{t.get('kind')}）：{t.get('progress') or '未开始'}，最近推进第 {t.get('last_ch', 0)} 章")
    return "\n".join(lines)


def character_brief(cards: list[CharacterCard]) -> str:
    if not cards:
        return "（暂无角色卡）"
    lines = []
    for c in cards:
        line = f"- {c.id}（{c.name}，{c.role}）：性格 {'、'.join(c.personality[:3]) or '未填'}；战力 {c.power_scale.rank or '未填'}"
        if c.power_scale.ceiling:
            line += f"（上限 {c.power_scale.ceiling}）"
        if c.power_scale.forbidden:
            line += f"；⚠{c.power_scale.forbidden}"
        if c.status != "active":
            line += f"；状态 {c.status}"
        lines.append(line)
    return "\n".join(lines)


# ================================================================ Writer

WRITER_SYSTEM_TEMPLATE = """你是《{title}》的执笔者。这是一部{genre}网文。严格遵守以下全部约束：

## 世界观红线（L0）
{constitution}

## 文风约束（L0）
{style}

## 写作要求
- 字数 {min_words}–{max_words}（按中文字符计）
- 视角：{pov} 第三人称限制视角，不得写出该角色不可能知道的信息
- 必须包含：{must_include}
- 结尾钩子：{hook}
- 禁止：照抄「相关旧文」原句；使用与人物卡/世界观冲突的旧设定；角色做出违反行为边界的事
- 只输出小说正文（Markdown 段落），不要输出解释、大纲、标题以外的前后缀说明"""


# ================================================================ Reviewer

REVIEWER_SYSTEM = """你是网文平台的资深责编，负责挑错。**只挑可核查的硬伤**，不做主观文采评价。

检查维度（9 类，逐类过一遍）：
| category | 检查内容 | 依据 |
|---|---|---|
| OOC | 言行/决策违反角色卡 boundaries 或 personality | 角色卡 |
| power | 战力表现违反 power_scale / forbidden | 角色卡 + 力量体系 |
| timeline | 章内事件与时间线冲突 | timeline.yaml |
| pov | 视角漂移（写出 POV 角色不可能知道的信息） | 章纲 pov |
| repetition | 复读（句式/口癖超频、跨章重复桥段） | 口癖表 + 近章 |
| ai-flavor | AI 高频词、排比堆砌、空洞比喻 | 文风指纹规则 |
| hook | 结尾钩子缺失/无力 | 章纲 hook + 平台 profile |
| redline | 平台红线词、敏感词 | 平台词库 |
| words | 字数区间 | 平台 profile |

输出格式（严格 JSON）：
{"issues":[{"severity":"high|mid|low","category":"...","evidence":"原文引用（≤60字）","suggestion":"具体改法"}]}

规则：
- evidence 必须是正文里真实存在的原文片段，不许编造
- 没有问题就返回 {"issues":[]}
- 不要输出 JSON 以外的任何内容"""


def reviewer_prompt(
    *,
    ch: int,
    text: str,
    outline: ChapterOutline,
    profile: PlatformProfile,
    characters: list[CharacterCard],
    timeline: str,
    style: str,
    recent_summaries: str,
    word_count: int,
) -> str:
    char_lines = []
    for c in characters:
        char_lines.append(
            f"**{c.name}**（{c.id}）\n"
            f"- 性格：{'、'.join(c.personality) or '未填'}\n"
            f"- 口癖：{'；'.join(c.speech_habits) or '未填'}\n"
            f"- 行为边界：{'；'.join(c.boundaries) or '未填'}\n"
            f"- 战力：{c.power_scale.rank or '未填'}"
            + (f"；⚠{c.power_scale.forbidden}" if c.power_scale.forbidden else "")
        )
    return f"""# 待审章节
第 {ch} 章「{outline.title}」
- 计划字数：{outline.target_words[0]}–{outline.target_words[1]}，实际：{word_count} 字
- POV：{outline.pov}
- 计划钩子：{outline.hook}
- 必须包含：{'; '.join(outline.must_include) or '（无）'}

# 平台要求（{profile.platform}）
- 字数区间：{profile.chapter_words[0]}–{profile.chapter_words[1]}
- 钩子：{'必须有，位置 ' + profile.hook_position if profile.hook_required else '不强制'}
- 红线词：{', '.join(profile.redline_words[:30]) or '（无词库）'}
- 敏感检查：{profile.sensitive_check}

# 出场人物卡（OOC/power 判定依据）
{chr(10).join(char_lines) or '（无）'}

# 时间线
{timeline[:1200] or '（无记录）'}

# 文风指纹（ai-flavor 判定依据）
{style[:1200] or '（无）'}

# 近章摘要（repetition 判定依据）
{recent_summaries[:1500] or '（无）'}

# 正文
<<<
{text}
>>>

逐类检查后输出 JSON。"""


# ================================================================ Curator

CURATOR_SYSTEM = """你是这本书的档案管理员（Curator）。你的职责是把刚完成的一章**沉淀成结构化记忆**。

铁律：
1. 你只产出**提案**（proposals），绝不直接改文件；作者确认后才落盘
2. 摘要只写事实与因果，不复述原文，不写评价
3. 伏笔动作必须与章纲一致；原文没写的不要脑补
4. 人物状态变化只在原文明确写出时才提议（如境界突破、获得物品、受伤、死亡）
5. 只输出 JSON，不要解释

输出格式：
{"proposals":[
  {"id":"p-<ch>-1","file":"memory/ch-XXXX.summary.md","op":"create","content":"...","reason":"章后沉淀"},
  {"id":"p-<ch>-2","file":"truth/foreshadow.json","op":"patch","patch":{"F-001":{"status":"hinted","hinted_chs":[42]}},"reason":"..."},
  {"id":"p-<ch>-3","file":"truth/timeline.yaml","op":"append","patch":{"ch":42,"date":"玄历1024年四月初二","event":"..."},"reason":"..."},
  {"id":"p-<ch>-4","file":"truth/world-state.md","op":"patch","section":"主角处境","content":"...","reason":"..."},
  {"id":"p-<ch>-5","file":"truth/characters/mc.yaml","op":"patch","patch":{"power_scale":{"rank":"淬体八重"}},"reason":"..."},
  {"id":"p-<ch>-6","file":"memory/entity-deltas.jsonl","op":"append","lines":["{\\"ch\\":42,\\"entity\\":\\"mc\\",\\"kind\\":\\"state\\",\\"op\\":\\"update\\",\\"note\\":\\"...\\"}"],"reason":"..."}
]}

注意：
- 只要这一章真的发生了，才产出对应提案；没有就少产出，不要凑数
- 摘要 150–300 字
- 伏笔 patch 的键是伏笔 id，值是**要改的字段**（不是整条覆盖）
- entity-deltas 每行一个 JSON 对象，字段：ch / entity / kind(state|relation|power|location) / op(add|update|remove) / target / type / note"""


def curator_prompt(
    *,
    ch: int,
    title: str,
    text: str,
    outline: ChapterOutline,
    foreshadow_brief: str,
    timeline_brief: str,
    world_state: str,
    characters_brief: str,
) -> str:
    return f"""# 第 {ch} 章「{title}」已完成，请沉淀

## 章纲（本章计划）
- POV：{outline.pov}
- 场景：{json.dumps([s.model_dump() for s in outline.scenes], ensure_ascii=False)[:1500]}
- 计划伏笔动作：{json.dumps([a.model_dump() for a in outline.foreshadow_actions], ensure_ascii=False)}
- 计划钩子：{outline.hook}

## 正文章节
<<<
{text[:12000]}
>>>

## 当前伏笔状态
{foreshadow_brief or '（无）'}

## 当前时间线（末尾）
{timeline_brief or '（无）'}

## 当前世界状态
{world_state[:1000]}

## 人物卡摘要
{characters_brief[:1500]}

请产出提案 JSON。"""


# ================================================================ Deconstructor

DECONSTRUCTOR_SYSTEM = """你是网文拆书分析师。分析对标书的**结构性规律**，不复制任何原文。

合规红线：产物只保留抽象结构数据（节奏曲线、钩子模式占比、爽点间隔），绝不输出原文句子。

输出格式（严格 JSON）：
{"structure":{"volumes":[{"index":int,"chapters":int,"note":str}],"coolpoint_curve":[float],"opening_pattern":str,"golden_finger":str,"power_up_interval_ch":int},
 "pacing":{"avg_words":int,"dialogue_ratio":float,"action_ratio":float,"info_release":str,"conflict_interval_ch":int},
 "hooks":{"types":{"危机型":float,"悬念型":float,"反转型":float,"情绪型":float},"best_examples":"（只写模式描述，不引原文）"},
 "template":{"genre":str,"opening_hook_type":str,"arc_length_ch":int,"notes":str}}"""


def deconstructor_prompt(*, title: str, chapter_texts: list[str], max_chars: int = 60000) -> str:
    body_parts: list[str] = []
    used = 0
    for i, t in enumerate(chapter_texts, 1):
        piece = f"\n--- 第 {i} 段（章）---\n{t[:4000]}"
        if used + len(piece) > max_chars:
            break
        body_parts.append(piece)
        used += len(piece)
    return f"""# 待拆解：《{title}》
共提供 {len(body_parts)} 章样本。

{''.join(body_parts)}

请分析结构、节奏、钩子配比，输出 JSON。"""


# ================================================================ 解析工具


def parse_chapters(payload: dict[str, Any]) -> list[ChapterOutline]:
    """把 Planner 返回的 JSON 转成 ChapterOutline 列表（宽容：缺字段补默认）。"""
    raw = payload.get("chapters") or payload.get("data") or []
    if isinstance(raw, dict):
        raw = [raw]
    out: list[ChapterOutline] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        item = dict(item)
        item.setdefault("pov", "mc")
        item.setdefault("target_words", [2000, 2400])
        item.setdefault("status", "draft")
        try:
            out.append(ChapterOutline.model_validate(item))
        except Exception:
            continue
    out.sort(key=lambda o: o.ch)
    return out


def parse_issues(payload: dict[str, Any], *, allowed: set[str] | None = None) -> list[ReviewIssue]:
    raw = payload.get("issues") or []
    out: list[ReviewIssue] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        cat = str(item.get("category", "ai-flavor"))
        if allowed and cat not in allowed:
            cat = "ai-flavor"
        sev = str(item.get("severity", "mid"))
        if sev not in ("high", "mid", "low"):
            sev = "mid"
        try:
            out.append(
                ReviewIssue(
                    severity=sev,  # type: ignore[arg-type]
                    category=cat,  # type: ignore[arg-type]
                    evidence=str(item.get("evidence", ""))[:300],
                    suggestion=str(item.get("suggestion", ""))[:500],
                    loc=str(item.get("loc", ""))[:200],
                )
            )
        except Exception:
            continue
    return out
