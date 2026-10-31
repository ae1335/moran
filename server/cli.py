"""命令行入口（M1 要求：MVP 期不写一行前端也能用 CLI 跑通）。

用法：
    python -m server.cli init --root ./ws
    python -m server.cli new "凡人问道" --genre 玄幻 --platform tomato
    python -m server.cli books
    python -m server.cli plan --book novel-《凡人问道》 --count 3
    python -m server.cli confirm 1 2 3
    python -m server.cli write 1
    python -m server.cli review 1
    python -m server.cli curate 1
    python -m server.cli curate-confirm 1
    python -m server.cli assemble 1
    python -m server.cli export --mode 合集
    python -m server.cli panel
    python -m server.cli usage
    python -m server.cli index build
    python -m server.cli pipeline --chapters 1-3   # 一条命令跑完 plan→write→review→curate→confirm
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

# 允许直接 `python server/cli.py`
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _root(args) -> Path:
    return Path(args.root or os.environ.get("NOVEL_WORKBENCH_ROOT") or Path.cwd()).resolve()


def _ctx(args):
    from server.api.deps import AppContext

    return AppContext.create(_root(args))


def _book(ctx, args):
    key = args.book
    if not key:
        shelf = ctx.workbench.refresh_shelf()
        if len(shelf.novels) != 1:
            raise SystemExit(f"工作区有 {len(shelf.novels)} 本书，请用 --book 指定")
        key = shelf.novels[0].dir
    return ctx.find_book(key)


def _print(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


# ---------------------------------------------------------------- 子命令


def cmd_init(args) -> int:
    ctx = _ctx(args)
    from server.model.providers.registry import write_default_configs

    written = write_default_configs(ctx.workbench.paths.config_dir)
    print(f"工作区已初始化: {ctx.root}")
    print(f"  配置: {ctx.workbench.paths.config_dir} ({', '.join(written) or '已存在'})")
    print(f"  书籍: {ctx.workbench.paths.novels}")
    return 0


def cmd_new(args) -> int:
    from server.core.gitbot import GitBot
    from server.core.schema import BookMeta, CharacterCard

    ctx = _ctx(args)
    book = BookMeta(
        title=args.title,
        genre=args.genre,
        platform=args.platform,
        target={
            "total_chapters": args.total,
            "daily_chapters": args.daily,
            "words_per_chapter": (args.words_min, args.words_max),
        },
    )
    store = ctx.workbench.create_novel(book, template=args.template or None)
    store.save_character(
        CharacterCard(
            id="mc",
            name=args.protagonist,
            role="protagonist",
            personality=["外冷内热", "记仇", "对承诺病态执着"],
            boundaries=["绝不主动屠杀平民", "不接受胁迫下立的誓言"],
            power_scale={"rank": "淬体七重", "ceiling": "淬体九重", "forbidden": "本卷内不得突破凝气期"},
            first_chapter=1,
            last_chapter=0,
        )
    )
    gb = GitBot(store.root)
    gb.init()
    gb.commit_all(f"init: 建书《{args.title}》")
    print(f"已创建《{book.title}》 → {store.root}")
    return 0


def cmd_books(args) -> int:
    ctx = _ctx(args)
    shelf = ctx.workbench.refresh_shelf()
    if not shelf.novels:
        print("（还没有书，用 `new` 创建）")
        return 0
    for n in shelf.novels:
        print(f"{n.dir:30s} {n.title:12s} ch={n.current_chapter:<5d} {n.platform:8s} {n.status}")
    return 0


def cmd_plan(args) -> int:
    bc = _book(_ctx(args), args)
    res = asyncio.run(bc.planner.plan(start_ch=args.start, count=args.count, save=True))
    for c in res.chapters:
        print(f"ch-{c.ch:04d} {c.title:8s} pov={c.pov} scenes={len(c.scenes)} hook={c.hook[:30]}")
    for w in res.warnings:
        print(f"  ⚠ {w}")
    for a in res.alerts:
        print(f"  {'⛔' if a['level'] == 'overdue' else '⏳'} {a['id']} {a['desc']} ({a.get('suggestion', '')})")
    print(f"\n已落盘 {res.saved} 章草案（状态 draft，需 confirm 才进入写作）")
    return 0


def cmd_confirm(args) -> int:
    """确认章纲：draft / planned / writing → planned。

    缺陷 #72/#73 之后这条路径不能再无条件赋值：终态（reviewed 之后）被拉回
    planned 会让 daily-panel 的存稿口径凭空多一章。这里复用 planner 的
    confirm_outline，它内部查 OUTLINE_TRANSITIONS，非法迁移直接报错。
    """
    from server.agents.planner import confirm_outline as _confirm

    bc = _book(_ctx(args), args)
    bad = 0
    for ch in args.chapters:
        try:
            o = _confirm(bc.store, ch)
        except ValueError as exc:
            print(f"ch-{ch:04d} 无法确认：{exc}")
            bad += 1
            continue
        print(f"ch-{ch:04d} → {o.status}")
    return 1 if bad else 0


def cmd_write(args) -> int:
    bc = _book(_ctx(args), args)
    res = asyncio.run(bc.writer.write(args.ch, as_version=args.as_version))
    print(f"ch-{res.ch:04d} 已生成 {res.words} 字（model={res.model}, version={res.version}）")
    print(f"装配审计: {res.assembled_path}")
    for w in res.warnings:
        print(f"  ⚠ {w}")
    return 0


def cmd_review(args) -> int:
    bc = _book(_ctx(args), args)
    res = asyncio.run(bc.reviewer.review(args.ch, use_model=not args.no_model))
    print(f"ch-{args.ch:04d} 审稿：{len(res.report.issues)} 个问题（{res.report.model}）")
    for i in res.report.issues:
        print(f"  [{i.severity:4s}] {i.category:10s} {i.evidence[:40]!r} → {i.suggestion[:60]}")
    if res.path:
        print(f"报告: {res.path}")
    return 0


def cmd_curate(args) -> int:
    bc = _book(_ctx(args), args)
    res = asyncio.run(bc.curator.curate(args.ch, use_model=not args.no_model))
    print(f"ch-{args.ch:04d} 提案 {len(res.pack.proposals)} 条：")
    for p in res.pack.proposals:
        print(f"  [{p.op:6s}] {p.file}  ({p.reason})")
    for w in res.warnings:
        print(f"  ⚠ {w}")
    print("\n用 `curate-confirm` 落盘")
    return 0


def cmd_curate_confirm(args) -> int:
    bc = _book(_ctx(args), args)
    out = bc.curator.confirm(args.ch, args.ids or None, commit=not args.no_commit)
    # 缺陷 #114：一条都没落下去（或落盘报错）时，原来这里照样 return 0 ——
    # 和 #113 同一类问题：告警只进了 warnings，没走到进程出口。
    failed = int(out.get("failed") or 0)
    print(f"已应用 {len(out['applied'])} 项，跳过 {len(out['skipped'])} 项" + (f"，失败 {failed} 项" if failed else ""))
    for w in out["warnings"]:
        print(f"  ⚠ {w}")
    if out.get("commit"):
        c = out["commit"]
        print(f"git: {'ok ' + (c.get('sha') or 'noop') if c.get('ok') else '失败 ' + str(c.get('error'))}")
    if failed:
        print("=== 有提案落盘失败，未全部沉淀 ===")
    return 1 if failed else 0


def cmd_assemble(args) -> int:
    from server.context.assembler import save_assembly

    bc = _book(_ctx(args), args)
    o = bc.store.load_outline(args.ch)
    if o is None:
        raise SystemExit(f"ch-{args.ch:04d} 章纲不存在")
    ctx_obj = asyncio.run(bc.assembler.assemble(o))
    save_assembly(bc.store, ctx_obj)
    print(f"装配完成：{ctx_obj.total_tokens} / {ctx_obj.budget} tokens")
    for k, l in ctx_obj.layers.items():
        print(f"  {k} {l.title:16s} {l.tokens:6d} {'[裁剪]' if l.trimmed else ''}")
    for w in ctx_obj.warnings:
        print(f"  ⚠ {w}")
    print(f"审计: {bc.store.rel(bc.store.p.assembled_md(args.ch))}")
    if args.show_prompt:
        print("\n===== SYSTEM =====\n" + ctx_obj.system_prompt)
        print("\n===== USER =====\n" + ctx_obj.user_prompt)
    return 0


def cmd_export(args) -> int:
    bc = _book(_ctx(args), args)
    res = bc.exporter.export(mode=args.mode, allow_risk=args.allow_risk, file_format=args.file_format)
    if res.blocked:
        print(f"⛔ {res.block_reason}")
        return 1
    print(f"导出 {res.chapters} 章 / {res.total_words} 字 → {res.out_dir}")
    for f in res.files:
        print(f"  {f}")
    highs = [c for c in res.checks if c.high_issues]
    if highs:
        print(f"\n⚠ {len(highs)} 章有未处理的 high 问题：")
        for c in highs:
            for i in c.high_issues:
                print(f"  ch-{c.ch:04d} [{i['category']}] {i['suggestion']}")
    return 0


def cmd_setting_book(args) -> int:
    bc = _book(_ctx(args), args)
    res = bc.setting_book.export(include_chapters=args.with_chapters)
    print(f"设定集 → {res.out_dir}（{res.words} 字，{len(res.sections)} 节）")
    for f in res.files:
        print(f"  {f}")
    for w in res.warnings:
        print(f"  ⚠ {w}")
    return 0


def cmd_cover(args) -> int:
    bc = _book(_ctx(args), args)
    res = bc.cover.generate(width=args.width, height=args.height)
    print(f"封面 → {res.path}（{res.width}×{res.height}，{res.format}）")
    print(f"  提示词：{res.prompt}")
    for w in res.warnings:
        print(f"  ⚠ {w}")
    return 0


def cmd_publish(args) -> int:
    bc = _book(_ctx(args), args)
    res = bc.exporter.mark_published(args.chapters, published=not args.unpublish)
    print(("标记发布" if not args.unpublish else "撤销发布") + f": {res['updated']}")
    for item in res["skipped"]:
        print(f"  跳过第 {item['ch']} 章：{item['reason']}")
    # 缺陷 #108：#70 让服务端把断层「说出口」（gaps / warning），#105 让 Web 端
    # 消费它，但 CLI 只打印 updated —— 走终端发布的作者看到的仍是「标记发布: [5]」，
    # 而读者端 1~4 章是空的。同一条告警不能在某个入口被丢弃。
    if res.get("warning"):
        print(f"  ⚠ {res['warning']}")
    return 0 if res["updated"] or not res["skipped"] else 1


def cmd_panel(args) -> int:
    bc = _book(_ctx(args), args)
    p = bc.exporter.daily_panel()
    print(f"《{p['book']}》 当前第 {p['current_chapter']} 章 · 总 {p['total_words']} 字")
    print(f"存稿 draft={p['stock_count']['draft']} reviewed={p['stock_count']['reviewed']} final={p['stock_count']['final']}")
    print(f"可支撑 {p['stock_days']} 天（目标日更 {p['daily_target']} 章）")
    if p["shortage_warning"]:
        print(f"⚠ {p['warning_text']}")
    print(f"今日队列: {p['today_queue']}")
    return 0


def cmd_name(args) -> int:
    """起名生成器（§10.3）：角色/功法/宗门/地名。"""
    bc = _book(_ctx(args), args)
    try:
        pool = bc.naming.generate(
            kind=args.kind, count=args.count, seed=args.seed,
            gender=args.gender, save=not args.no_save,
        )
    except ValueError as exc:
        print(f"错误：{exc}")
        return 2
    label = {"character": "角色", "skill": "功法/招式", "sect": "宗门/势力", "place": "地名"}[args.kind]
    print(f"{label}（seed={pool.seed}）：")
    for c in pool.candidates:
        note = f"  — {c.note}" if c.note else ""
        print(f"  {c.name}{note}")
    if pool.skipped:
        print(f"（跳过 {len(pool.skipped)} 个与已有角色重名：{'、'.join(pool.skipped[:5])}）")
    if not args.no_save:
        print(f"\n已追加到 materials/dictionary/names-pool.md")
    return 0


def cmd_timeline(args) -> int:
    """查看/校验时间线（M1 闭环命令行需要，不必开 Web）。"""
    from .core.domain import date_to_days, parse_fiction_date, validate_timeline

    bc = _book(_ctx(args), args)
    entries = bc.store.load_timeline()
    if not entries:
        print("时间线为空（确认沉淀提案后会自动追加）")
        return 0
    issues = validate_timeline(entries)
    for e in entries:
        d = parse_fiction_date(e.date)
        days = f"{date_to_days(d):>8d}" if d else "       ?"
        print(f"  ch{e.ch:04d} {days}  {e.date}  {e.event}")
    if issues:
        print(f"\n⚠ {len(issues)} 个校验问题：")
        for i in issues:
            print(f"  [{getattr(i, 'level', 'warn')}] {getattr(i, 'message', i)}")
        return 1
    print(f"\n✓ {len(entries)} 条，时间单调且与章号一致")
    return 0


def cmd_foreshadow(args) -> int:
    """查看伏笔池（状态机当前分布 + 超期报警）。"""
    from .core.domain import overdue_alerts

    bc = _book(_ctx(args), args)
    fb = bc.store.load_foreshadow()
    book = bc.store.load_book()
    if getattr(args, "action", False):
        print("状态流转请用 Web UI 或 REST（CLI 只读，避免误改关键事实）")
    if not fb.items:
        print("伏笔池为空。章纲里写 foreshadow_actions 并确认沉淀后会自动登记。")
        return 0
    order = {"draft": 0, "planted": 1, "hinted": 2, "resolved": 3, "dropped": 4}
    for it in sorted(fb.items, key=lambda x: (order.get(x.status, 9), x.id)):
        mark = {"planted": "○", "hinted": "◐", "resolved": "●", "dropped": "×", "draft": "·"}.get(it.status, "?")
        print(f"  {mark} {it.id:10s} {it.status:9s} 埋{it.planted_ch:>3} 示{it.hinted_chs or '-'} 截止{it.deadline_ch}  {it.desc[:36]}")
    alerts = overdue_alerts(fb, book.current_chapter)
    if alerts:
        print(f"\n⚠ {len(alerts)} 条超期/临期：")
        for a in alerts:
            print(f"  {a['id']}：{a['desc'][:30]}（{a['reason']}，剩 {a['remaining']} 章）")
    return 0


def cmd_usage(args) -> int:
    ctx = _ctx(args)
    u = ctx.usage.usage(days=args.days)
    t = u["total"]
    print(f"近 {args.days} 天：{t['calls']} 次调用 · {t['tokens']} tokens · ¥{t['cost']:.4f} · 错误 {t['errors']}")
    print(f"订阅池占比: {u['subscription_share']:.0%}")
    print("\n按任务:")
    for k, v in sorted(u["by_task"].items()):
        print(f"  {k:12s} calls={v['calls']:<5d} tokens={v['tokens']:<8d} ¥{v['cost']:.4f}")
    print("\n按供应商:")
    for k, v in sorted(u["by_provider"].items()):
        print(f"  {k:20s} calls={v['calls']:<5d} tokens={v['tokens']:<8d} ¥{v['cost']:.4f}")
    return 0


def cmd_index(args) -> int:
    bc = _book(_ctx(args), args)
    if bc.retrieval is None:
        print("检索层未启用（server/retrieval 不可用）")
        return 1
    if args.action == "build":
        print(json.dumps(bc.retrieval.build_index(full=args.full), ensure_ascii=False, indent=2))
    elif args.action == "verify":
        # 缺陷 #119：verify 是给 cron / CI 用的，ok=False 必须体现在退出码上。
        # 否则「半夜对账发现漂移」在收件箱里只是一段 JSON，谁也不会看。
        rep = bc.retrieval.verify()
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        if not rep.get("ok"):
            for key, label in (("drifted", "正文已改未重索引"),
                                ("missing", "正文有、索引没有"),
                                ("orphaned", "索引有、正文已删"),
                                ("entity_source", "实体数据源"),
                                # 缺陷 #127：embedding 没跑成时索引照样「hash 全对」，
                                # 但语义检索是假的，cron 必须能看见。
                                ("vector_source", "向量缺失")):
                val = rep.get(key)
                if val:
                    print("  ⚠ %s：%s" % (label, val))
            return 1
    else:
        print(json.dumps(bc.retrieval.stats(), ensure_ascii=False, indent=2))
    return 0


def cmd_pipeline(args) -> int:
    """一条命令跑完：plan → confirm → write → review → curate → confirm。"""
    from server.core.gitbot import GitBot

    bc = _book(_ctx(args), args)
    gw = bc.gateway

    lo, hi = args.chapters
    print(f"=== 流水线 ch-{lo:04d}..ch-{hi:04d} ===")
    failed = 0

    # 1) 章纲：缺失的先 plan
    missing = [c for c in range(lo, hi + 1) if bc.store.load_outline(c) is None]
    if missing:
        print(f"[1/6] Planner 生成 {len(missing)} 章章纲…")
        asyncio.run(bc.planner.plan(start_ch=missing[0], count=len(missing), save=True))
    else:
        print("[1/6] 章纲已存在，跳过")

    # 2) 确认
    print("[2/6] 确认章纲…")
    for ch in range(lo, hi + 1):
        o = bc.store.load_outline(ch)
        if o and o.status == "draft":
            o.status = "planned"
            bc.store.save_outline(o)

    # 3) 写作
    print("[3/6] Writer 生成正文…")
    for ch in range(lo, hi + 1):
        try:
            res = asyncio.run(bc.writer.write(ch))
            print(f"  ch-{ch:04d} {res.words} 字")
        except Exception as exc:
            failed += 1  # 缺陷 #113：失败章要汇总到退出码，不能只 print
            print(f"  ch-{ch:04d} 失败: {exc}")

    # 4) 审稿
    print("[4/6] Reviewer 审稿…")
    for ch in range(lo, hi + 1):
        try:
            r = asyncio.run(bc.reviewer.review(ch, use_model=not args.no_model))
            print(f"  ch-{ch:04d} {len(r.report.issues)} 个问题")
        except Exception as exc:
            failed += 1  # 缺陷 #113：失败章要汇总到退出码，不能只 print
            print(f"  ch-{ch:04d} 失败: {exc}")

    # 5) 沉淀
    print("[5/6] Curator 产出提案…")
    for ch in range(lo, hi + 1):
        try:
            c = asyncio.run(bc.curator.curate(ch, use_model=not args.no_model))
            print(f"  ch-{ch:04d} {len(c.pack.proposals)} 条提案")
        except Exception as exc:
            failed += 1  # 缺陷 #113：失败章要汇总到退出码，不能只 print
            print(f"  ch-{ch:04d} 失败: {exc}")

    # 6) 落盘 + git
    print("[6/6] 确认落盘 + git 提交…")
    for ch in range(lo, hi + 1):
        try:
            out = bc.curator.confirm(ch, commit=True)
            o = bc.store.load_outline(ch)
            words = bc.store.chapter_word_count(ch)
            cres = GitBot(bc.store.root).commit_chapter(ch, o.title if o else "", words)
            # 缺陷 #126：commit 成功但 tag 没打上时不能照说「已提交」——
            # tag 是时光机 ch/0042 的唯一索引，打标失败等于这一章在时光机里查不到。
            tail = "" if not cres.tag_error else f"，⚠ 打标失败（时光机查不到该章）：{cres.tag_error}"
            print(f"  ch-{ch:04d} 应用 {len(out['applied'])} 项，已提交{tail}")
        except Exception as exc:
            failed += 1  # 缺陷 #113：失败章要汇总到退出码，不能只 print
            print(f"  ch-{ch:04d} 失败: {exc}")

    if failed:
        # 缺陷 #113：原来这里无条件打「=== 完成 ===」且 return 0。流水线里每一章
        # 的失败都被 try/except 吞成一行 print，脚本/CI 拿到的退出码永远是 0 ——
        # 半夜批量跑 20 章，19 章炸了，crontab 的邮件里只有「完成」。
        print(f"=== 完成（{failed} 步失败，见上方『失败』行）===")
    else:
        print("=== 完成 ===")
    log = GitBot(bc.store.root).log(limit=10)
    for e in log:
        print(f"  {e['short']} {e['message']}")
    return 1 if failed else 0


def cmd_serve(args) -> int:
    import logging

    import uvicorn

    # 定时任务失败只体现在 JobResult 返回值里，cron 触发时没人读它；
    # 不配 handler 就只能等 root logger 兜底，容易变成静默失败（§7.6）。
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    os.environ["NOVEL_WORKBENCH_ROOT"] = str(_root(args))
    uvicorn.run(
        "server.app:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )
    return 0


# ---------------------------------------------------------------- 解析


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="novel-workbench", description="网文工作台 CLI")
    p.add_argument("--root", help="工作区根目录（默认当前目录）")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="初始化工作区与配置文件").set_defaults(func=cmd_init)

    s = sub.add_parser("new", help="新建一本书")
    s.add_argument("title")
    s.add_argument("--genre", default="玄幻-东方玄幻")
    s.add_argument("--platform", default="tomato")
    s.add_argument("--total", type=int, default=1200)
    s.add_argument("--daily", type=int, default=2)
    s.add_argument("--words-min", type=int, default=2000)
    s.add_argument("--words-max", type=int, default=2400)
    s.add_argument("--protagonist", default="林澈")
    s.add_argument("--template", default="", help="题材模板 id（留空则按 genre 自动匹配）")
    s.set_defaults(func=cmd_new)

    sub.add_parser("books", help="列出所有书").set_defaults(func=cmd_books)

    s = sub.add_parser("plan", help="生成章纲草案")
    s.add_argument("--book")
    s.add_argument("--start", type=int, default=None)
    s.add_argument("--count", type=int, default=3)
    s.set_defaults(func=cmd_plan)

    s = sub.add_parser("confirm", help="确认章纲（draft → planned）")
    s.add_argument("chapters", type=int, nargs="+")
    s.add_argument("--book")
    s.set_defaults(func=cmd_confirm)

    s = sub.add_parser("write", help="生成正文")
    s.add_argument("ch", type=int)
    s.add_argument("--book")
    s.add_argument("--as-version", action="store_true")
    s.set_defaults(func=cmd_write)

    s = sub.add_parser("review", help="审稿")
    s.add_argument("ch", type=int)
    s.add_argument("--book")
    s.add_argument("--no-model", action="store_true")
    s.set_defaults(func=cmd_review)

    s = sub.add_parser("curate", help="生成沉淀提案")
    s.add_argument("ch", type=int)
    s.add_argument("--book")
    s.add_argument("--no-model", action="store_true")
    s.set_defaults(func=cmd_curate)

    s = sub.add_parser("curate-confirm", help="确认提案落盘")
    s.add_argument("ch", type=int)
    s.add_argument("--book")
    s.add_argument("--ids", nargs="*")
    s.add_argument("--no-commit", action="store_true")
    s.set_defaults(func=cmd_curate_confirm)

    s = sub.add_parser("assemble", help="只跑装配，看上下文")
    s.add_argument("ch", type=int)
    s.add_argument("--book")
    s.add_argument("--show-prompt", action="store_true")
    s.set_defaults(func=cmd_assemble)

    s = sub.add_parser("export", help="导出发布包")
    s.add_argument("--book")
    s.add_argument("--mode", default="合集", choices=["合集", "分章"])
    s.add_argument("--file-format", default="txt", choices=["txt", "docx"])
    s.add_argument("--allow-risk", action="store_true")
    s.set_defaults(func=cmd_export)

    s = sub.add_parser("publish", help="手动标记章节已发布（只记录，不代发）")
    s.add_argument("chapters", nargs="+", type=int)
    s.add_argument("--book")
    s.add_argument("--unpublish", action="store_true", help="撤销发布标记，恢复为定稿")
    s.set_defaults(func=cmd_publish)

    s = sub.add_parser("setting-book", help="导出设定集（M8）")
    s.add_argument("--book")
    s.add_argument("--with-chapters", action="store_true", help="附上章节正文全文")
    s.set_defaults(func=cmd_setting_book)

    s = sub.add_parser("cover", help="生成封面（M8，默认程序化 SVG）")
    s.add_argument("--book")
    s.add_argument("--width", type=int, default=600)
    s.add_argument("--height", type=int, default=800)
    s.set_defaults(func=cmd_cover)

    s = sub.add_parser("panel", help="日更面板")
    s.add_argument("--book")
    s.set_defaults(func=cmd_panel)

    s = sub.add_parser("name", help="起名生成器（角色/功法/宗门/地名）")
    s.add_argument("kind", nargs="?", default="character",
                   choices=["character", "skill", "sect", "place"], help="起名类别")
    s.add_argument("-n", "--count", type=int, default=10)
    s.add_argument("--seed", type=int, default=None, help="固定 seed 可复现")
    s.add_argument("--gender", default="", choices=["", "male", "female"], help="角色性别倾向")
    s.add_argument("--no-save", action="store_true", help="只看不写入词池")
    s.add_argument("--book")
    s.set_defaults(func=cmd_name)

    s = sub.add_parser("timeline", help="查看并校验时间线")
    s.add_argument("--book")
    s.set_defaults(func=cmd_timeline)

    s = sub.add_parser("foreshadow", help="查看伏笔池与超期报警")
    s.add_argument("--book")
    s.set_defaults(func=cmd_foreshadow)

    s = sub.add_parser("usage", help="用量统计")
    s.add_argument("--days", type=int, default=7)
    s.set_defaults(func=cmd_usage)

    s = sub.add_parser("index", help="检索索引")
    s.add_argument("action", choices=["build", "verify", "stats"])
    s.add_argument("--book")
    s.add_argument("--full", action="store_true")
    s.set_defaults(func=cmd_index)

    s = sub.add_parser("pipeline", help="一键跑完 plan→write→review→curate→confirm")
    s.add_argument("--book")
    s.add_argument("--chapters", default="1-1", help="形如 1-3")
    s.add_argument("--no-model", action="store_true")
    s.set_defaults(func=cmd_pipeline)

    s = sub.add_parser("serve", help="启动 Web 服务")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--reload", action="store_true")
    s.set_defaults(func=cmd_serve)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.cmd == "pipeline":
        lo, _, hi = str(args.chapters).partition("-")
        try:
            args.chapters = (int(lo), int(hi or lo))
        except ValueError:
            raise SystemExit("--chapters 格式应为 1-3")
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
