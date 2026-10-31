"""题材模板库（§10.2）：拆书产物的 template.json 供新书初始化复用。

设计边界：
- 模板**不是真相源**，只影响 `_seed_truth` 的种子文本（开局模式、爽点节奏、升级间隔）。
- 模板库在工作区级 `workbench/analysis/templates/`，跨书共享（§4 工作区根另设 workbench/）。
- 书内 `analysis/templates/` 是拆书时的镜像，读取时优先工作区级（共享优先，符合 M8「共享模板」）。
- 模板缺失/损坏一律降级为内置默认种子，绝不阻断建书。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import atomic

TEMPLATE_SUFFIX = ".json"


@dataclass
class GenreTemplate:
    """一份题材模板。字段全部可选——拆书启发式未必填得满。"""

    id: str = ""
    genre: str = ""
    opening_pattern: str = ""
    opening_hook_type: str = ""
    golden_finger: str = ""
    arc_length_ch: int = 0
    power_up_interval_ch: int = 0
    coolpoint_interval_ch: int = 0
    notes: str = ""
    source: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "genre": self.genre,
            "opening_pattern": self.opening_pattern,
            "opening_hook_type": self.opening_hook_type,
            "golden_finger": self.golden_finger,
            "arc_length_ch": self.arc_length_ch,
            "power_up_interval_ch": self.power_up_interval_ch,
            "coolpoint_interval_ch": self.coolpoint_interval_ch,
            "notes": self.notes,
            "source": self.source,
        }

    def brief(self) -> str:
        """给 Planner 的一段提示（§10.2：章纲生成时注入节奏基线）。"""
        bits: list[str] = []
        if self.opening_pattern:
            bits.append(f"开局模式：{self.opening_pattern}")
        if self.opening_hook_type:
            bits.append(f"主导断章钩型：{self.opening_hook_type}")
        if self.golden_finger:
            bits.append(f"金手指：{self.golden_finger}")
        if self.arc_length_ch:
            bits.append(f"单卷章数基线：{self.arc_length_ch}")
        if self.power_up_interval_ch:
            bits.append(f"升级间隔基线：约 {self.power_up_interval_ch} 章")
        if self.coolpoint_interval_ch:
            bits.append(f"爽点间隔基线：≤{self.coolpoint_interval_ch} 章")
        if self.notes:
            bits.append(f"备注：{self.notes}")
        return "；".join(bits)


def _coerce_int(value: Any, default: int = 0) -> int:
    try:
        n = int(float(value))
    except (TypeError, ValueError):
        return default
    return n if n > 0 else default


def parse_template(raw: dict[str, Any], *, id: str = "") -> GenreTemplate:
    """把拆书产物或手写 JSON 宽松解析成模板（脏数据不抛）。"""
    if not isinstance(raw, dict):
        return GenreTemplate(id=id)
    struct = raw.get("structure") if isinstance(raw.get("structure"), dict) else {}
    pacing = raw.get("pacing") if isinstance(raw.get("pacing"), dict) else {}
    merged = {**struct, **pacing, **raw}
    return GenreTemplate(
        id=str(raw.get("id") or raw.get("source") or id),
        genre=str(merged.get("genre") or ""),
        opening_pattern=str(merged.get("opening_pattern") or ""),
        opening_hook_type=str(merged.get("opening_hook_type") or ""),
        golden_finger=str(merged.get("golden_finger") or ""),
        arc_length_ch=_coerce_int(merged.get("arc_length_ch")),
        power_up_interval_ch=_coerce_int(merged.get("power_up_interval_ch")),
        coolpoint_interval_ch=_coerce_int(merged.get("coolpoint_interval_ch")),
        notes=str(merged.get("notes") or ""),
        source=str(raw.get("source") or id),
        raw=raw,
    )


class TemplateLibrary:
    """模板库读取。工作区级优先，其次书内镜像（§4 / §10.2）。"""

    def __init__(self, shared_dir: Path, book_dir: Path | None = None):
        self.shared_dir = Path(shared_dir)
        self.book_dir = Path(book_dir) if book_dir else None

    # ---------------- 读取 ----------------
    def _dirs(self) -> list[Path]:
        dirs = [self.shared_dir]
        if self.book_dir is not None and self.book_dir != self.shared_dir:
            dirs.append(self.book_dir)
        return dirs

    def list(self) -> list[GenreTemplate]:
        """列出全部模板，同 id 时共享库优先（跨书共享语义）。"""
        out: dict[str, GenreTemplate] = {}
        for d in reversed(self._dirs()):  # 先书内，再共享覆盖
            if not d.exists():
                continue
            for f in sorted(d.glob(f"*{TEMPLATE_SUFFIX}")):
                try:
                    raw = json.loads(atomic.read_text(f) or "{}")
                except (json.JSONDecodeError, OSError):
                    continue
                tpl = parse_template(raw, id=f.stem)
                if not tpl.id:
                    tpl.id = f.stem
                out[tpl.id] = tpl
        return sorted(out.values(), key=lambda t: t.id)

    def get(self, key: str) -> GenreTemplate | None:
        key = (key or "").strip()
        if not key:
            return None
        if key in ("", "none", "-", "default"):
            return None
        for tpl in self.list():
            if tpl.id == key:
                return tpl
        # 退化：按题材名匹配（作者可能直接传 "玄幻-东方玄幻"）
        for tpl in self.list():
            if tpl.genre and tpl.genre == key:
                return tpl
        return None

    def match_genre(self, genre: str) -> GenreTemplate | None:
        """建书时按题材自动挑一份最接近的模板（前缀匹配，宁缺毋滥）。"""
        genre = (genre or "").strip()
        if not genre:
            return None
        tpls = self.list()
        for tpl in tpls:
            if tpl.genre and tpl.genre == genre:
                return tpl
        for tpl in tpls:
            if tpl.genre and (genre.startswith(tpl.genre) or tpl.genre.startswith(genre)):
                return tpl
        return None

    # ---------------- 写入（共享库） ----------------
    def save(self, tpl: GenreTemplate) -> Path:
        self.shared_dir.mkdir(parents=True, exist_ok=True)
        path = self.shared_dir / f"{tpl.id or 'template'}{TEMPLATE_SUFFIX}"
        atomic.write_json_atomic(path, tpl.raw or tpl.as_dict())
        return path
