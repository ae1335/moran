"""封面生成（M8）。

分两条路，**默认离线可跑**：

1. **SVG 程序化封面**（默认）：按书名/题材/主角名排版生成矢量封面，
   零依赖、零调用、立即出图。适合占位与投稿前的临时封面。
2. **图像 API**（可选）：配置了 `image_api` 才启用。§7.3 的红线同样适用——
   订阅桥必须回环，官方图像 API 才允许公网。

设计边界：
- 产物入 `exports/covers/`，**不进 git**（exports/ 已在 .gitignore）。
- SVG 里的文字做 XML 转义，避免书名带 `&`/`<` 打坏文件。
- 封面不是真相源，重新生成不产生副作用。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import html
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core import atomic
from ..core.store import NovelStore

# 题材 → 配色（背景渐变 + 前景色）。键按前缀匹配，越靠前越具体。
GENRE_THEMES: list[tuple[str, dict[str, str]]] = [
    ("玄幻", {"bg1": "#1a1626", "bg2": "#2d1f3d", "fg": "#e8d5a3", "accent": "#c9a227"}),
    ("仙侠", {"bg1": "#0f1e1c", "bg2": "#1b3a33", "fg": "#d8e8d5", "accent": "#7fbf9a"}),
    ("都市", {"bg1": "#101418", "bg2": "#1e2a36", "fg": "#e6edf3", "accent": "#5aa9e6"}),
    ("科幻", {"bg1": "#0a0e17", "bg2": "#141c2e", "fg": "#d6e4f0", "accent": "#00d4ff"}),
    ("悬疑", {"bg1": "#12100f", "bg2": "#241a18", "fg": "#e8dcd4", "accent": "#c0392b"}),
    ("历史", {"bg1": "#1a1510", "bg2": "#2e2418", "fg": "#e8ddc8", "accent": "#b8860b"}),
    ("言情", {"bg1": "#241218", "bg2": "#3d1f2d", "fg": "#f0dde5", "accent": "#e08fa8"}),
]
DEFAULT_THEME = {"bg1": "#14161a", "bg2": "#22262e", "fg": "#e6e8eb", "accent": "#c9a227"}


@dataclass
class CoverResult:
    path: str = ""
    format: str = "svg"
    width: int = 600
    height: int = 800
    source: str = "procedural"
    prompt: str = ""
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "format": self.format,
            "width": self.width,
            "height": self.height,
            "source": self.source,
            "prompt": self.prompt,
            "warnings": self.warnings,
        }


def theme_for(genre: str) -> dict[str, str]:
    genre = genre or ""
    for key, theme in GENRE_THEMES:
        if key in genre:
            return theme
    return DEFAULT_THEME


def _wrap_title(title: str, per_line: int = 4) -> list[str]:
    """书名按每行 N 字折行（中文书名一般 3–6 字，超过就折）。"""
    title = (title or "").strip()
    if not title:
        return [""]
    return [title[i : i + per_line] for i in range(0, len(title), per_line)][:3]


def build_prompt(store: NovelStore) -> str:
    """拼一段图像 API 的提示词（作者可复制到任何生图工具用）。"""
    book = store.load_book()
    hero_name = ""
    hero = store.get_character(book.protagonist) if book.protagonist else None
    if hero is None:
        hero = next((c for c in store.list_characters() if c.role == "protagonist"), None)
    if hero is not None:
        hero_name = hero.name
    bits = [f"中文网络小说封面", f"书名《{book.title}》"]
    if book.genre:
        bits.append(f"题材：{book.genre}")
    if hero_name:
        bits.append(f"主角：{hero_name}")
    bits.append("竖版构图，留出书名排版空间，不用文字")
    return "；".join(bits)


class CoverGenerator:
    """封面生成器。"""

    def __init__(self, store: NovelStore, image_api: dict[str, Any] | None = None):
        self.store = store
        self.image_api = image_api or {}

    @property
    def out_dir(self) -> Path:
        return self.store.p.exports / "covers"

    # ---------------- SVG ----------------
    def render_svg(self, width: int = 600, height: int = 800) -> str:
        book = self.store.load_book()
        theme = theme_for(book.genre)
        title_lines = _wrap_title(book.title)
        hero = self.store.get_character(book.protagonist) if book.protagonist else None
        if hero is None:
            hero = next((c for c in self.store.list_characters() if c.role == "protagonist"), None)
        hero_name = hero.name if hero else ""

        # 标题块整体垂直居中，书名最多 3 行
        n = len(title_lines)
        line_h = 76
        block_h = n * line_h
        start_y = (height - block_h) // 2 + 40
        title_tspans = "".join(
            f'<tspan x="{width // 2}" y="{start_y + i * line_h}">{html.escape(line)}</tspan>'
            for i, line in enumerate(title_lines)
        )

        subtitle = html.escape(book.genre or "")
        hero_line = html.escape(f"主角 · {hero_name}") if hero_name else ""
        stamp = dt.date.today().strftime("%Y")

        # 装饰：左侧竖线 + 底部横线，用 accent 色
        return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="0.6" y2="1">
      <stop offset="0%" stop-color="{theme['bg1']}"/>
      <stop offset="100%" stop-color="{theme['bg2']}"/>
    </linearGradient>
    <linearGradient id="fade" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="{theme['accent']}" stop-opacity="0"/>
      <stop offset="50%" stop-color="{theme['accent']}" stop-opacity="0.5"/>
      <stop offset="100%" stop-color="{theme['accent']}" stop-opacity="0"/>
    </linearGradient>
  </defs>

  <rect width="{width}" height="{height}" fill="url(#bg)"/>

  <!-- 竖向装饰线 -->
  <rect x="48" y="70" width="2" height="{height - 140}" fill="url(#fade)"/>
  <!-- 顶部小字 -->
  <text x="72" y="96" fill="{theme['accent']}" font-family="serif" font-size="15" letter-spacing="6" opacity="0.85">{subtitle}</text>

  <!-- 书名 -->
  <text text-anchor="middle" fill="{theme['fg']}" font-family="serif" font-size="62" font-weight="600" letter-spacing="6">
    {title_tspans}
  </text>

  <!-- 分隔线 -->
  <line x1="{width // 2 - 60}" y1="{start_y + block_h + 4}" x2="{width // 2 + 60}" y2="{start_y + block_h + 4}" stroke="{theme['accent']}" stroke-width="1.5" opacity="0.7"/>

  <!-- 主角行 -->
  <text x="{width // 2}" y="{start_y + block_h + 46}" text-anchor="middle" fill="{theme['accent']}" font-family="serif" font-size="20" letter-spacing="4">{hero_line}</text>

  <!-- 底部标记 -->
  <text x="{width // 2}" y="{height - 54}" text-anchor="middle" fill="{theme['fg']}" font-family="serif" font-size="13" letter-spacing="3" opacity="0.45">{stamp}</text>
</svg>
"""

    # ---------------- 主入口 ----------------
    def generate(
        self,
        *,
        width: int = 600,
        height: int = 800,
        save: bool = True,
        use_image_api: bool = False,
    ) -> CoverResult:
        prompt = build_prompt(self.store)
        warnings: list[str] = []

        if use_image_api and self.image_api:
            # 图像 API 分支：本实现不内置任何第三方 SDK，只预留结构化入口。
            # 作者配好自己的 endpoint 后由调用方注入 callable（见 server/api/deps.py）。
            warnings.append("已请求图像 API，但当前未配置可用的图像 API 适配器，回退为程序化 SVG")
            use_image_api = False

        svg = self.render_svg(width=width, height=height)
        res = CoverResult(
            format="svg", width=width, height=height, source="procedural", prompt=prompt, warnings=warnings
        )
        if not save:
            return res

        self.out_dir.mkdir(parents=True, exist_ok=True)
        book = self.store.load_book()
        stem = re.sub(r"[^\w\u4e00-\u9fff-]+", "-", book.title or "cover")[:40] or "cover"
        digest = hashlib.sha1(svg.encode("utf-8")).hexdigest()[:8]
        path = self.out_dir / f"cover-{stem}-{digest}.svg"
        atomic.write_text_atomic(path, svg)
        res.path = self.store.rel(path)

        # 同时写一份提示词清单，方便作者去外部工具出图
        meta_path = self.out_dir / "prompts.json"
        try:
            existing = json.loads(atomic.read_text(meta_path) or "{}")
        except json.JSONDecodeError:
            existing = {}
        if not isinstance(existing, dict):
            existing = {}
        existing[book.title] = {
            "prompt": prompt,
            "svg": res.path,
            "updated": dt.datetime.now().isoformat(timespec="seconds"),
        }
        atomic.write_json_atomic(meta_path, existing)
        return res
