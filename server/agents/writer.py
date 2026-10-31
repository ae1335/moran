"""Writer（§6.2）：装配后的 Prompt → 正文（SSE 流式，可中断，章粒度幂等）。"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from ..context.assembler import AssembledContext, ContextAssembler, save_assembly
from ..core import atomic
from ..core.schema import ChapterOutline
from ..core.store import NovelStore, count_cn_words
from ..model.base import CompletionRequest, Message, StreamChunk
from ..model.gateway import ModelGateway


@dataclass
class WriteResult:
    ch: int
    text: str
    words: int
    version: int
    model: str = ""
    assembled_path: str = ""
    warnings: list[str] = field(default_factory=list)
    total_tokens: int = 0
    adopted: bool = False


def clean_prose(text: str) -> str:
    """清掉模型爱加的围栏 / 前置说明，保留正文与章标题。"""
    s = text.strip()
    # 去 ``` 围栏
    m = re.match(r"^```(?:markdown|md)?\s*\n(.*?)\n```\s*$", s, re.S)
    if m:
        s = m.group(1).strip()
    # 去"好的，以下是..."这类前置语
    s = re.sub(
        r"^(好的|没问题|以下是|这是)[^\n]{0,60}\n+", "", s, count=1
    ).strip()
    # 去掉模型自造的「写作说明」尾巴
    s = re.split(r"\n(?:---|\*\*\*)\s*\n(?:以上|（完）|全文完)", s)[0].strip()
    return s


class Writer:
    def __init__(self, store: NovelStore, gateway: ModelGateway, assembler: ContextAssembler | None = None):
        self.store = store
        self.gw = gateway
        self.assembler = assembler or ContextAssembler(store)

    async def prepare(self, ch: int) -> AssembledContext:
        outline = self.store.load_outline(ch)
        if outline is None:
            raise ValueError(f"第 {ch} 章章纲不存在，先跑 Planner 或手写章纲")
        if not outline.scenes and not outline.hook:
            raise ValueError(f"第 {ch} 章章纲过于空（无 scenes 无 hook），无法生成")
        ctx = await self.assembler.assemble(outline)
        save_assembly(self.store, ctx)
        return ctx

    def _warnings(self, ctx: AssembledContext, outline: ChapterOutline, words: int) -> list[str]:
        """装配告警 + 字数偏少/偏多。非流式 write() 与流式 write_stream() 共用同一口径（缺陷 #122）。"""
        warnings = list(ctx.warnings)
        lo, hi = outline.target_words
        if words < lo * 0.8:
            warnings.append(f"字数偏少：{words} < 目标 {lo}–{hi}（可重生成或手动补写）")
        elif words > hi * 1.3:
            warnings.append(f"字数偏多：{words} > 目标 {lo}–{hi}")
        return warnings

    # ---------------- 非流式 ----------------
    async def write(
        self,
        ch: int,
        *,
        save: bool = True,
        as_version: bool = False,
        temperature: float | None = None,
    ) -> WriteResult:
        outline = self.store.load_outline(ch)
        if outline is None:
            raise ValueError(f"第 {ch} 章章纲不存在")
        if outline.status == "published":
            raise ValueError(f"第 {ch} 章已发布；如需修改请先在日更面板撤销发布标记")
        ctx = await self.prepare(ch)
        req = CompletionRequest(
            task="write",
            messages=[
                Message(role="system", content=ctx.system_prompt),
                Message(role="user", content=ctx.user_prompt),
            ],
            temperature=temperature,
            max_tokens=8192,
            chapter=ch,
        )
        res = await self.gw.complete(req)
        text = clean_prose(res.text)
        words = count_cn_words(text)
        warnings = self._warnings(ctx, outline, words)

        version = 0
        if save:
            version = self._persist(ch, text, res.model, as_version=as_version)
        return WriteResult(
            ch=ch,
            text=text,
            words=words,
            version=version,
            model=res.model,
            assembled_path=self.store.rel(self.store.p.assembled_md(ch)),
            warnings=warnings,
            total_tokens=res.total_tokens,
            adopted=not as_version,
        )

    # ---------------- 流式 ----------------
    async def write_stream(
        self,
        ch: int,
        *,
        save: bool = True,
        as_version: bool = False,
        temperature: float | None = None,
    ) -> AsyncIterator[StreamChunk]:
        """流式产出。中断即停止；上层按章幂等重生成（从章首重来）。"""
        outline = self.store.load_outline(ch)
        if outline is None:
            raise ValueError(f"第 {ch} 章章纲不存在")
        if outline.status == "published":
            raise ValueError(f"第 {ch} 章已发布；如需修改请先在日更面板撤销发布标记")
        ctx = await self.prepare(ch)
        req = CompletionRequest(
            task="write",
            messages=[
                Message(role="system", content=ctx.system_prompt),
                Message(role="user", content=ctx.user_prompt),
            ],
            temperature=temperature,
            max_tokens=8192,
            chapter=ch,
            stream=True,
        )
        buf: list[str] = []
        model = ""
        async for chunk in self.gw.stream(req):
            if chunk.delta:
                buf.append(chunk.delta)
            if chunk.model:
                model = chunk.model
            if chunk.done:
                text = clean_prose("".join(buf))
                words = count_cn_words(text)
                if save and text.strip():
                    self._persist(ch, text, model, as_version=as_version)
                yield StreamChunk(
                    delta="",
                    done=True,
                    provider=chunk.provider,
                    model=model,
                    usage={
                        **chunk.usage,
                        "words": words,
                        "ch": ch,
                    },
                    warnings=self._warnings(ctx, outline, words),
                )
                return
            yield chunk

    # ---------------- 落盘 ----------------
    def _persist(self, ch: int, text: str, model: str, *, as_version: bool) -> int:
        import datetime as dt

        outline = self.store.load_outline(ch)
        words = count_cn_words(text)
        version = 0
        if as_version:
            version = self.store.next_version_number(ch)
            self.store.write_version(
                ch,
                version,
                text,
                {"created": dt.datetime.now().isoformat(timespec="seconds"), "model": model, "words": words},
            )
        else:
            # 已有正文时先留旧版（多版本留档，§6.2）
            if self.store.chapter_exists(ch):
                old = self.store.read_chapter(ch)
                if old.strip() and old.strip() != text.strip():
                    v = self.store.next_version_number(ch)
                    self.store.write_version(
                        ch,
                        v,
                        old,
                        {
                            "created": dt.datetime.now().isoformat(timespec="seconds"),
                            "model": "previous",
                            "words": count_cn_words(old),
                            "note": "被新版本覆盖前的旧稿",
                        },
                    )
            self.store.write_chapter(ch, text)
            version = 1
        if outline is not None and outline.status in ("planned", "draft", "writing"):
            outline.status = "drafted"  # type: ignore[assignment]
            self.store.save_outline(outline)
        # book.current_chapter 前推
        book = self.store.load_book()
        if book.current_chapter < ch:
            book.current_chapter = ch
            self.store.save_book(book)
        return version

    # ---------------- 多版本 ----------------
    def adopt_version(self, ch: int, version: int) -> dict[str, Any]:
        # 缺陷 #76：切换版本最终也是 write_chapter，和 write()/write_stream() 一样
        # 能改已发布章的正文。三处必须同一口径，否则「已发布章覆盖保护」只守两条路。
        o = self.store.load_outline(ch)
        if o is not None and o.status == "published":
            raise ValueError(
                f"第 {ch} 章已发布；如需切换版本请先撤销发布标记（已发布章正文不允许直接改写）"
            )
        d = self.store.p.chapter_versions_dir(ch)
        f = d / f"v{version}.md"
        if not f.exists():
            raise ValueError(f"第 {ch} 章没有 v{version}")
        text = f.read_text(encoding="utf-8")
        # 当前正文留档
        if self.store.chapter_exists(ch):
            cur = self.store.read_chapter(ch)
            if cur.strip() and cur.strip() != text.strip():
                nv = self.store.next_version_number(ch)
                self.store.write_version(
                    ch, nv, cur, {"model": "previous", "words": count_cn_words(cur), "note": "切换版本前留档"}
                )
        self.store.write_chapter(ch, text)
        meta = self.store.list_versions(ch)
        for m in meta:
            mfile = d / f"v{m['version']}.meta.json"
            # 缺陷 #84：同上，坏 meta 不该让「切换版本」整条失败
            data = atomic.read_json(mfile, None) or {}
            if not isinstance(data, dict):
                data = {}
            data["adopted"] = m["version"] == version
            atomic.write_text_atomic(mfile, json.dumps(data, ensure_ascii=False, indent=2))
        return {"ch": ch, "adopted": version, "words": count_cn_words(text)}

    def score_version(self, ch: int, version: int, score: int, note: str = "") -> dict[str, Any]:
        """评分回流（§2.2 多版本留档 + 评分回流）。"""
        if not 1 <= score <= 5:
            raise ValueError("评分必须 1–5")
        mfile = self.store.p.chapter_version_meta(ch, version)
        # 缺陷 #84：裸 json.loads(read_text()) 会让一个写坏的 meta 文件把整条评分
        # 回流链路堵死（实测返回 400 Expecting value，作者补不了分）。
        # meta 只是留档，读不出来就当空字典，本次写入会把它修好。
        data: dict[str, Any] = atomic.read_json(mfile, None) or {}
        if not isinstance(data, dict):
            data = {}
        data["score"] = score
        if note:
            data["note"] = note
        self.store.save_version_meta(ch, version, data)
        return {"ch": ch, "version": version, "score": score}
