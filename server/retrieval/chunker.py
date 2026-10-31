"""中文分块器（§8.1：文本 chunk 粒度 ≈500 字、段落对齐）。

设计要点
--------
- **段落是分块的最小单位**：按空行/换行切段，累积到 400–700 字收一块，绝不切断段落。
- **超长段落**（单段 > MAX_CHARS）按句末标点（。！？；…）二次切，绝不切在句子中间；
  单句本身超长时按 MAX_CHARS 硬切（宁可丢句读也不断索引）。
- **稳定**：同一章文本重复分块得到完全相同的 chunk_id / hash / offset。
  chunk_id 形如 ``c-0042-1``（章号四位零填充 + 章内 1-based 序号），不依赖随机数、
  不依赖 dict 顺序、不依赖当前时间。

``offset`` 是 chunk 首字符在**原章文本**中的字符偏移（切块时按原始行累计，不受
"用换行拼接同块多段"影响），``para_range`` 是所覆盖的原始段落下标区间（0-based，含两端）。
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from ..core.paths import ch_name
from ..core.store import count_cn_words

#: 目标块大小（字）
TARGET_CHARS = 500
#: 收块的软下限：达到这个量再考虑断开
MIN_CHARS = 400
#: 单块硬上限（不含超长段二次切后的边界抖动）
MAX_CHARS = 700

#: 句子切分：非终止符 + 终止符 + 尾部收引号/括号
_SENT_RE = re.compile(r"[^。！？!?…\n]+[。！？!?…]*[」』”’）)]*")


def hash_text(text: str) -> str:
    """sha1 前 16 位（chunk 与章的指纹口径一致）。"""
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def count_chars(text: str) -> int:
    """按网文口径计字（中文按字、英文/数字按词），与 NovelStore 同一口径。"""
    return count_cn_words(text)


def citation(ch: int, para_start: int, para_end: int | None = None) -> str:
    """出处标注：``[ch-0042:段3]``，跨段时 ``[ch-0042:段3-5]``（段号 1-based）。"""
    start = int(para_start) + 1
    end = start if para_end is None else int(para_end) + 1
    if end > start:
        return f"[{ch_name(ch)}:段{start}-{end}]"
    return f"[{ch_name(ch)}:段{start}]"


@dataclass(frozen=True)
class Chunk:
    """一个检索块。``chunk_id`` 稳定可复现。"""

    chunk_id: str
    ch: int
    offset: int
    text: str
    hash: str
    para_range: tuple[int, int]
    chars: int = 0

    @property
    def para_start(self) -> int:
        return self.para_range[0]

    @property
    def para_end(self) -> int:
        return self.para_range[1]

    def citation(self) -> str:
        return citation(self.ch, self.para_start, self.para_end)


# ------------------------------------------------------------------ 内部


def _paragraphs(text: str) -> list[tuple[str, int, int]]:
    """切段：返回 [(段文本, 原文字符偏移, 段下标)]，段下标只数非空段。"""
    out: list[tuple[str, int, int]] = []
    offset = 0
    pidx = 0
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if stripped:
            lead = len(line) - len(line.lstrip())
            out.append((stripped, offset + lead, pidx))
            pidx += 1
        offset += len(line)
    return out


def _split_long(text: str, offset: int) -> list[tuple[str, int]]:
    """超长段落按句末标点切；单句超长时硬切。返回 [(片段, 偏移)]。"""
    pieces: list[tuple[str, int]] = []
    for m in _SENT_RE.finditer(text):
        piece = m.group(0)
        if not piece.strip():
            continue
        pieces.append((piece, offset + m.start()))
    if not pieces:
        pieces = [(text, offset)]

    out: list[tuple[str, int]] = []
    for piece, off in pieces:
        while count_chars(piece) > MAX_CHARS:
            out.append((piece[:MAX_CHARS], off))
            off += MAX_CHARS
            piece = piece[MAX_CHARS:]
        if piece:
            out.append((piece, off))
    return out


def _units(text: str) -> list[tuple[str, int, int]]:
    """把段列表展开成"分块单元"：普通段整段一个单元，超长段先切再按 ~TARGET 合并。"""
    units: list[tuple[str, int, int]] = []
    for ptext, poff, pidx in _paragraphs(text):
        if count_chars(ptext) <= MAX_CHARS:
            units.append((ptext, poff, pidx))
            continue
        buf: list[str] = []
        buf_words = 0
        buf_off: int | None = None
        for piece, off in _split_long(ptext, poff):
            if buf_off is None:
                buf_off = off
            buf.append(piece)
            buf_words += count_chars(piece)
            if buf_words >= TARGET_CHARS:
                units.append(("".join(buf), buf_off, pidx))
                buf, buf_words, buf_off = [], 0, None
        if buf and buf_off is not None:
            units.append(("".join(buf), buf_off, pidx))
    return units


# ------------------------------------------------------------------ 主入口


def chunk_chapter(ch: int, text: str) -> list[Chunk]:
    """把一章正文切成 ``Chunk`` 列表。空文本返回 ``[]``。"""
    if not text or not text.strip():
        return []

    units = _units(text)
    chunks: list[Chunk] = []
    buf: list[tuple[str, int, int]] = []
    buf_words = 0

    def flush() -> None:
        nonlocal buf, buf_words
        if not buf:
            return
        piece_text = "\n".join(u[0] for u in buf)
        chunks.append(
            Chunk(
                chunk_id=f"c-{int(ch)}-{len(chunks) + 1}",
                ch=int(ch),
                offset=buf[0][1],
                text=piece_text,
                hash=hash_text(piece_text),
                para_range=(buf[0][2], buf[-1][2]),
                chars=count_chars(piece_text),
            )
        )
        buf = []
        buf_words = 0

    for unit in units:
        uw = count_chars(unit[0])
        # 已够软下限且再加就超硬上限 → 先收块
        if buf and buf_words >= MIN_CHARS and buf_words + uw > MAX_CHARS:
            flush()
        buf.append(unit)
        buf_words += uw
        if buf_words >= TARGET_CHARS:
            flush()
    flush()
    return chunks


def chunk_stats(chunks: list[Chunk]) -> dict[str, int]:
    """分块统计（自检用）。"""
    if not chunks:
        return {"chunks": 0, "chars": 0, "min": 0, "max": 0, "avg": 0}
    sizes = [c.chars or count_chars(c.text) for c in chunks]
    return {
        "chunks": len(chunks),
        "chars": sum(sizes),
        "min": min(sizes),
        "max": max(sizes),
        "avg": sum(sizes) // len(sizes),
    }
