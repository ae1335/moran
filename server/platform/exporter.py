"""发布导出（§11）。只导出不代发。

导出前强制跑：redline 扫描 → words 区间校验 → hook 检查 → 反 AI 味管线状态检查。
产物入 exports/{date}/，附导出报告（标注所有未处理 high 级问题）。
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core import atomic
from ..core.schema import PlatformProfile
from ..core.store import NovelStore, count_cn_words


@dataclass
class ChapterCheck:
    ch: int
    title: str = ""
    words: int = 0
    ok: bool = True
    high_issues: list[dict[str, Any]] = field(default_factory=list)
    mid_issues: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class ExportResult:
    platform: str
    out_dir: str
    files: list[str]
    chapters: int
    total_words: int
    checks: list[ChapterCheck] = field(default_factory=list)
    report_path: str = ""
    risk_ack_required: bool = False
    blocked: bool = False
    block_reason: str = ""


class Exporter:
    def __init__(self, store: NovelStore, polish: Any | None = None):
        self.store = store
        # 缺陷 #123：导出预检要真的扫一遍套语（§9.2「发布前强制」）。polish 是可选
        # 依赖，传 None 就退化成不扫 —— 保持 Exporter(store) 的老调用方式可用。
        self._polish = polish

    # ---------------- 预检 ----------------
    def precheck(self, chapters: list[int], profile: PlatformProfile) -> list[ChapterCheck]:
        out: list[ChapterCheck] = []
        book = self.store.load_book()
        for ch in chapters:
            text = self.store.read_chapter(ch)
            outline = self.store.load_outline(ch)
            title = outline.title if outline else ""
            words = count_cn_words(text)
            check = ChapterCheck(ch=ch, title=title, words=words)

            if not text.strip():
                check.ok = False
                check.high_issues.append({"category": "empty", "evidence": "", "suggestion": "正文为空"})
                out.append(check)
                continue

            lo, hi = profile.chapter_words
            if words < lo:
                check.high_issues.append(
                    {"category": "words", "evidence": f"{words} 字", "suggestion": f"低于 {lo} 字"}
                )
            elif words > hi:
                check.mid_issues.append(
                    {"category": "words", "evidence": f"{words} 字", "suggestion": f"超过 {hi} 字，建议拆章"}
                )

            for w in profile.redline_words:
                if w and w in text:
                    check.high_issues.append(
                        {"category": "redline", "evidence": w, "suggestion": f"删除或替换「{w}」"}
                    )

            if profile.hook_required:
                tail = text[-400:]
                if not any(m in tail for m in ("？", "！", "突然", "却", "竟然", "忽然", "……", "没想到")):
                    check.mid_issues.append(
                        {"category": "hook", "evidence": tail[-40:], "suggestion": "结尾钩子偏弱"}
                    )

            if profile.title_max_chars and title and len(title) > profile.title_max_chars:
                check.mid_issues.append(
                    {
                        "category": "title",
                        "evidence": title,
                        "suggestion": f"章节名 {len(title)} 字，超过 {profile.title_max_chars} 字限制",
                    }
                )

            # 已有审稿报告：把未处理的 high 引进来（§11.1）
            report = self.store.load_review(ch)
            if report:
                for issue in report.issues:
                    if issue.severity == "high":
                        check.high_issues.append(
                            {"category": issue.category, "evidence": issue.evidence, "suggestion": issue.suggestion}
                        )

            # 缺陷 #123：反 AI 味管线「发布前强制」只查了个开关位 —— ai_flavor_pipeline
            # 真值 True 就放行，正文里的套语一条都没被扫。实测正文带 REPLACE_RULES 里
            # 的套语照样导出、blocked=False、产物里套语原样在。这里按规则层扫一遍，
            # 只报不改写（改写要作者点「润色」确认，导出不该偷偷改正文）。
            if self._polish is not None and text.strip():
                for issue in self._polish.scan(text, ch):
                    # 开关要真的管住扫描范围：ai_flavor_pipeline 关掉就没有规则层，
                    # repetition_check 关掉就没有跨章复读。开关名字骗人（只查位不跑管）
                    # 是缺陷 #123 的根因 —— 这里让两个开关各管各的。
                    if issue.kind == "复读" and not book.repetition_check:
                        continue
                    if issue.kind != "复读" and not book.ai_flavor_pipeline:
                        continue
                    item = {
                        "category": "ai-flavor",
                        "evidence": issue.original[:40],
                        "suggestion": f"{issue.kind}：{issue.suggestion}",
                    }
                    # 分级：套语与复读是「读者一眼能看出来」的硬伤（high，进导出报告
                    # 的 unresolved_high）；排比与节奏只是风格建议（mid）。全塞 high
                    # 会让导出报告被风格噪音淹没，反而没人看。
                    if issue.kind in ("替换", "复读"):
                        check.high_issues.append(item)
                    else:
                        check.mid_issues.append(item)
            check.ok = not check.high_issues
            out.append(check)
        return out

    # ---------------- 导出 ----------------
    def export(
        self,
        *,
        platform: str | None = None,
        chapters: list[int] | None = None,
        mode: str = "合集",
        allow_risk: bool = False,
        file_format: str = "txt",
    ) -> ExportResult:
        book = self.store.load_book()
        pname = platform or book.platform
        profile = self.store.load_profile(pname)
        file_format = file_format.lower().strip()
        if file_format not in ("txt", "docx"):
            raise ValueError("file_format 只支持 txt / docx")
        if file_format == "docx" and pname != "qidian":
            raise ValueError("DOCX 导出仅起点平台开放；其他平台请导出 TXT")
        chs = chapters or self.store.list_chapters()
        if not chs:
            raise ValueError("没有可导出的章节")

        # 管线状态检查（§9.2 / §11.1）
        risk_ack_required = not (book.ai_flavor_pipeline and book.repetition_check)
        if risk_ack_required and not allow_risk:
            return ExportResult(
                platform=pname,
                out_dir="",
                files=[],
                chapters=0,
                total_words=0,
                risk_ack_required=True,
                blocked=True,
                block_reason="反 AI 味管线或复读检测已被关闭，导出前需要一次不可撤销的风险确认（allow_risk=true）",
            )

        checks = self.precheck(chs, profile)
        date = dt.date.today().isoformat()
        out_dir = self.store.p.exports / date
        out_dir.mkdir(parents=True, exist_ok=True)

        files: list[str] = []
        total_words = 0
        suffix = "docx" if file_format == "docx" else "txt"
        # 合集
        parts: list[str] = []
        for check in checks:
            text = self.store.read_chapter(check.ch)
            total_words += check.words
            title = check.title or f"第{check.ch}章"
            body = _strip_leading_chapter_title(_strip_md_heading(text))
            if mode == "合集":
                parts.append(f"第{check.ch}章 {title}\n\n{body.strip()}\n")
            else:
                fname = f"ch-{check.ch:04d}.{suffix}"
                content = f"第{check.ch}章 {title}\n\n{body.strip()}\n"
                if profile.author_note_supported:
                    content += "\n\n—— 作者的话 ——\n\n"
                if file_format == "docx":
                    _write_docx(out_dir / fname, book.title, content)
                else:
                    (out_dir / fname).write_text(content, encoding="utf-8")
                files.append(fname)

        if mode == "合集":
            fname = f"{_safe(book.title)}_{pname}_{date}.{suffix}"
            body = "\n\n".join(parts)
            if profile.author_note_supported:
                body = body.rstrip() + "\n\n—— 作者的话 ——\n\n"
            if file_format == "docx":
                _write_docx(out_dir / fname, book.title, body)
            else:
                (out_dir / fname).write_text(body, encoding="utf-8")
            files.append(fname)

        # 导出报告
        report = {
            "platform": pname,
            "book": book.title,
            "date": date,
            "mode": mode,
            "format": file_format,
            "chapters": len(checks),
            "total_words": total_words,
            "files": files,
            "unresolved_high": [
                {"ch": c.ch, "title": c.title, "issues": c.high_issues} for c in checks if c.high_issues
            ],
            "mid_issues": [{"ch": c.ch, "issues": c.mid_issues} for c in checks if c.mid_issues],
            "risk_ack": risk_ack_required,
        }
        report_name = f"export-report-{date}.json"
        atomic.write_json_atomic(out_dir / report_name, report)
        files.append(report_name)

        return ExportResult(
            platform=pname,
            out_dir=self.store.rel(out_dir),
            files=files,
            chapters=len(checks),
            total_words=total_words,
            checks=checks,
            report_path=self.store.rel(out_dir / report_name),
            risk_ack_required=risk_ack_required,
        )

    # ---------------- 发布标记（§11.2：作者手动确认，不代发） ----------------
    def mark_published(self, chapters: list[int], *, published: bool = True) -> dict[str, Any]:
        """在 final ↔ published 间切换。

        本工具不访问平台；这里只记录作者已经在外部平台完成发布的事实。
        已发布章不再计入存稿，也不能被 Writer 静默覆盖。
        """
        from ..core.domain import can_outline_transition

        updated: list[int] = []
        skipped: list[dict[str, Any]] = []
        for ch in sorted(set(chapters)):
            outline = self.store.load_outline(ch)
            if outline is None:
                skipped.append({"ch": ch, "reason": "章纲不存在"})
                continue
            target = "published" if published else "final"
            if outline.status == target:
                continue
            if not can_outline_transition(outline.status, target):
                skipped.append({"ch": ch, "reason": f"不允许 {outline.status} → {target}；只有定稿章可标记发布"})
                continue
            outline.status = target  # type: ignore[assignment]
            self.store.save_outline(outline)
            updated.append(ch)
        gaps = self._published_gaps() if published else []
        out: dict[str, Any] = {
            "published": published,
            "updated": updated,
            "skipped": skipped,
            "gaps": gaps,
        }
        if gaps:
            # 不阻断：本工具只是记录作者在外部平台已经做完的事实，
            # 但读者视角是断的——必须说出口，否则作者以为一切正常。
            shown = "、".join(str(g) for g in gaps[:5])
            more = f" 等 {len(gaps)} 章" if len(gaps) > 5 else ""
            out["warning"] = f"已发布章节前面还有未发布的章（第 {shown} 章{more}），读者会看到断层"
        return out

    def _published_gaps(self) -> list[int]:
        """找出「已发布章之前还空着的章号」。

        网文是按章连载的：读者点进第 5 章时，第 1~4 章必须已经可读。
        缺任何一个就是断层，而断层在作者端看不出来——他自己的书架上都在。
        """
        outlines = self.store.list_outlines()
        published = {o.ch for o in outlines if o.status == "published"}
        if not published:
            return []
        frontier = max(published)
        have = {o.ch for o in outlines}
        return [c for c in range(1, frontier) if c not in published and c in have]

    # ---------------- 日更面板（§11.2） ----------------
    def daily_panel(self) -> dict[str, Any]:
        book = self.store.load_book()
        outlines = self.store.list_outlines()
        by_status: dict[str, list[int]] = {}
        for o in outlines:
            by_status.setdefault(o.status, []).append(o.ch)

        daily_target = max(1, book.target.daily_chapters)
        # 三级存稿箱
        draft = sorted(by_status.get("drafted", []))
        reviewed = sorted(by_status.get("reviewed", []))
        final = sorted(by_status.get("final", []))
        published = sorted(by_status.get("published", []))
        # planned 优先于尚未确认的 draft，符合「按优先级」而非混合后排章号
        planned = sorted(by_status.get("planned", [])) + sorted(by_status.get("draft", []))
        # 存稿 = 已写完但还没发布的章节；final 计入，published 明确排除。
        stock = len(draft) + len(reviewed) + len(final)
        days_left = stock / daily_target
        warn = days_left < 3

        # 字数曲线（最近 30 章）
        chapters = self.store.list_chapters()[-30:]
        curve = [{"ch": c, "words": count_cn_words(self.store.read_chapter(c))} for c in chapters]

        # 连更天数（按 git 提交日期粗算；无 git 时按章节数）
        streak = self._streak_days()

        return {
            "book": book.title,
            "platform": book.platform,
            "current_chapter": book.current_chapter,
            "today_queue": planned[:10],
            "stock": {"draft": draft, "reviewed": reviewed, "final": final, "published": published},
            "stock_count": {
                "draft": len(draft), "reviewed": len(reviewed),
                "final": len(final), "published": len(published),
            },
            "stock_days": round(days_left, 1),
            "stock_breakdown": {
                "drafted": len(draft),
                "reviewed": len(reviewed),
                "final_unpublished": len(final),
                "published": len(published),
                "total": stock,
            },
            "daily_target": daily_target,
            "shortage_warning": warn,
            "warning_text": (
                f"存稿仅够 {days_left:.1f} 天（目标 ≥3 天），断更风险高" if warn else ""
            ),
            "words_curve": curve,
            "streak_days": streak,
            "total_words": sum(count_cn_words(self.store.read_chapter(c)) for c in self.store.list_chapters()),
            "pacing": self._pacing_comparison(),
        }

    def _pacing_comparison(self) -> dict[str, Any]:
        """与拆书基线的节奏对比数据（§11.2）。 stats 已具备计算，面板直接复用。"""
        from ..stats.writing import WritingStats

        return WritingStats(self.store).pacing_vs_benchmark()

    def _streak_days(self) -> int:
        from ..core.gitbot import GitBot

        gb = GitBot(self.store.root)
        log = gb.log(limit=200)
        if not log:
            return 0
        days = sorted({e["date"][:10] for e in log if e["message"].startswith("ch-")}, reverse=True)
        if not days:
            return 0
        streak = 0
        cur = dt.date.today()
        day_set = set(days)
        while cur.isoformat() in day_set:
            streak += 1
            cur -= dt.timedelta(days=1)
        return streak


def _strip_md_heading(text: str) -> str:
    """导出纯 TXT：去掉 markdown 标题语法与粗体标记。"""
    out = re.sub(r"^#{1,6}\s*", "", text, flags=re.MULTILINE)
    out = out.replace("**", "").replace("__", "")
    return out


#: 正文首行的章标题形态（导出会自己拼一个，必须先剥掉，否则作者拿到重复标题）
_LEADING_TITLE_RE = re.compile(r"^\s*第\s*[0-9一二三四五六七八九十百千]+\s*章\s*.*$")


def _strip_leading_chapter_title(text: str) -> str:
    """去掉正文开头重复的「第N章 标题」行。

    章文件是 markdown（首行 ``# 第N章 标题``），导出时会统一生成标题，
    不去掉就会出现「第1章 淬骨」连写两遍（实测复现）。
    """
    lines = text.lstrip("\n").splitlines()
    if lines and _LEADING_TITLE_RE.match(lines[0]):
        lines = lines[1:]
    return "\n".join(lines).lstrip("\n")


def _safe(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name.strip())


def _write_docx(path: Path, title: str, text: str) -> None:
    """用标准库生成最小可打开的 DOCX，不引入 python-docx 依赖。

    段落以换行拆分；章标题沿用正文内容，不额外重复书名。
    导出仍需经过与 TXT 相同的 redline / words / hook 预检。
    """
    import zipfile
    from xml.sax.saxutils import escape

    paragraphs = []
    for line in text.splitlines() or [""]:
        value = escape(line)
        paragraphs.append(
            "<w:p><w:r><w:rPr><w:rFonts w:eastAsia=\"宋体\"/><w:sz w:val=\"24\"/></w:rPr>"
            f"<w:t xml:space=\"preserve\">{value}</w:t></w:r></w:p>"
        )
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{''.join(paragraphs)}<w:sectPr/></w:body></w:document>"
    )
    content_types = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>"""
    rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""
    tmp = path.with_suffix(path.suffix + ".tmp")
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", rels)
        zf.writestr("word/document.xml", document)
    tmp.replace(path)

