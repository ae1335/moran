"""定时任务（§14.1 APScheduler）：夜审 / 索引重建 / 备份。

默认全部关闭，需要在 workbench-config/scheduler.yaml 里显式开启——
单人本地工具，不应该默认跑后台任务。
"""
from __future__ import annotations

import copy
import datetime as dt
import inspect
import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml

logger_name = "workbench.scheduler"
logger = logging.getLogger(logger_name)

DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "jobs": {
        "nightly_review": {"enabled": False, "cron": "0 3 * * *", "note": "夜间批审所有 drafted 章"},
        "index_rebuild": {"enabled": False, "cron": "30 3 * * *", "note": "索引增量重建 + hash 对账"},
        "backup": {"enabled": False, "cron": "0 4 * * *", "note": "工作区备份到 backup_dir，保留 N 份"},
    },
    "backup_dir": "backups",
    "backup_keep": 7,
}


@dataclass
class JobResult:
    job: str
    ok: bool
    detail: str = ""
    data: dict[str, Any] = field(default_factory=dict)


def load_scheduler_config(config_dir: Path) -> dict[str, Any]:
    """读 scheduler.yaml；缺文件时返回默认配置的**副本**。

    必须 deepcopy：DEFAULTS 是模块级对象，直接返回（或浅拷贝）会把内层
    jobs 字典的引用漏给调用方，任何就地修改都会跨请求、跨测试污染全局默认。
    """
    f = Path(config_dir) / "scheduler.yaml"
    if not f.exists():
        return copy.deepcopy(DEFAULTS)
    raw = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
    # 缺陷 #98：scheduler.yaml 顶层写成标量/列表时 `{**DEFAULTS, **raw}` 抛
    # TypeError（list object is not a mapping），起服务即崩。给中文报错，
    # 不静默回退——静默回退会让「配了夜审却从没跑过」无从察觉。
    if not isinstance(raw, dict):
        raise ValueError(
            f"scheduler.yaml 格式不对：顶层必须是键值表，当前是 {type(raw).__name__}。"
            f"请检查 {f}（删掉它即恢复默认）"
        )
    cfg = {**copy.deepcopy(DEFAULTS), **raw}
    raw_jobs = raw.get("jobs") or {}
    # 缺陷 #98：jobs 段写成列表时 `{**..., **list}` 抛 TypeError，起服务即崩。
    if not isinstance(raw_jobs, dict):
        raise ValueError(
            f"scheduler.yaml 的 jobs 段必须是键值表，当前是 {type(raw_jobs).__name__}。"
            f"请检查 {f}"
        )
    cfg["jobs"] = {**copy.deepcopy(DEFAULTS["jobs"]), **raw_jobs}
    return cfg


def _unique_dir(root: Path, stamp: str) -> Path:
    """同一秒内连跑两次备份也不能撞车：重名就追加 -2/-3 后缀。

    时间戳只到秒，而 copytree 的目标已存在会直接抛 FileExistsError，
    于是一秒内第二次备份必然失败——这不是备份策略问题，是路径生成缺陷。
    """
    dest = root / stamp
    n = 2
    while dest.exists():
        dest = root / f"{stamp}-{n}"
        n += 1
    return dest


class JobRunner:
    """把三个定时任务做成可独立调用的函数（Scheduler 与 HTTP 都调它）。"""

    def __init__(self, ctx):
        self.ctx = ctx

    # ---------------- 夜审 ----------------
    async def nightly_review(self, book: str | None = None) -> JobResult:
        targets: list[str] = []
        if book:
            targets = [book]
        else:
            targets = [n.dir for n in self.ctx.workbench.refresh_shelf().novels]

        summary: dict[str, Any] = {}
        failed = 0
        for d in targets:
            bc = self.ctx.find_book(d)
            drafted = [o.ch for o in bc.store.list_outlines() if o.status == "drafted"]
            rows = []
            for ch in drafted:
                try:
                    r = await bc.reviewer.review(ch, use_model=True)
                    rows.append({"ch": ch, "issues": len(r.report.issues)})
                except Exception as exc:
                    failed += 1
                    rows.append({"ch": ch, "error": f"{type(exc).__name__}: {exc}"})
                    # 失败不阻断：这一章记日志继续，其余章照审（§7.6）
                    logger.exception("夜审失败：%s 第 %s 章", d, ch)
            summary[d] = rows
        ok = failed == 0
        detail = f"审了 {len(targets)} 本书" + (f"，{failed} 章失败" if failed else "")
        if not ok:
            logger.warning("夜审部分失败：%s", detail)
        return JobResult(job="nightly_review", ok=ok, detail=detail, data=summary)

    # ---------------- 索引重建 ----------------
    def index_rebuild(self, book: str | None = None) -> JobResult:
        targets = [book] if book else [n.dir for n in self.ctx.workbench.refresh_shelf().novels]
        out: dict[str, Any] = {}
        failed = 0
        for d in targets:
            bc = self.ctx.find_book(d)
            if bc.retrieval is None:
                out[d] = {"skipped": "检索层未启用"}
                continue
            try:
                built = bc.retrieval.build_index(full=False)
                verified = bc.retrieval.verify()
                out[d] = {"build": built, "verify": verified}
                if isinstance(verified, dict) and not verified.get("ok", True):
                    failed += 1
                    logger.warning("索引对账不通过：%s %s", d, verified)
            except Exception as exc:
                failed += 1
                out[d] = {"error": f"{type(exc).__name__}: {exc}"}
                logger.exception("索引重建失败：%s", d)
        return JobResult(job="index_rebuild", ok=failed == 0, data=out)

    # ---------------- 备份 ----------------
    def backup(self) -> JobResult:
        cfg = load_scheduler_config(self.ctx.workbench.paths.config_dir)
        keep = max(1, int(cfg.get("backup_keep", 7)))
        dest_root = Path(cfg.get("backup_dir") or "backups")
        if not dest_root.is_absolute():
            dest_root = self.ctx.root / dest_root
        dest_root.mkdir(parents=True, exist_ok=True)
        dest = _unique_dir(dest_root, dt.datetime.now().strftime("%Y%m%d-%H%M%S"))
        try:
            shutil.copytree(
                self.ctx.workbench.paths.novels,
                dest / "novels",
                ignore=shutil.ignore_patterns(".rag-index", "exports", "__pycache__", "*.tmp"),
            )
            # 只保留最近 N 份（文件名带时间戳，字典序即时间序）。
            # 缺陷 #92：只认「我们自己写的备份目录」——目录名必须是 YYYYmmdd-HHMMSS
            # 或其 -2/-3 变体，且里面必须有 novels/ 子目录。否则备份区里作者自己
            # 放的任意文件夹（`0-我的重要资料`）会在字典序上排到时间戳前面被 rmtree 删掉。
            import re as _re

            pat = _re.compile(r"^\d{8}-\d{6}(-\d+)?$")
            ours = [
                p
                for p in dest_root.iterdir()
                if p.is_dir() and pat.match(p.name) and (p / "novels").exists()
            ]
            existing = sorted(ours, key=lambda p: p.name)
            if len(existing) > keep:
                for old in existing[: len(existing) - keep]:
                    shutil.rmtree(old, ignore_errors=True)
                logger.info("备份保留 %s 份，清理 %s 份旧备份", keep, len(existing) - keep)
            size = sum(f.stat().st_size for f in dest.rglob("*") if f.is_file())
            return JobResult(job="backup", ok=True, detail=f"{dest} ({size / 1024 / 1024:.1f} MB)", data={"path": str(dest)})
        except Exception as exc:
            logger.exception("备份失败：%s", dest)
            shutil.rmtree(dest, ignore_errors=True)  # 半成品目录不留在备份区
            return JobResult(job="backup", ok=False, detail=f"{type(exc).__name__}: {exc}")


def _report(name: str, res: Any) -> Any:
    """cron 触发的任务没人读返回值，失败必须写日志才算「失败不静默」（§7.6）。"""
    if isinstance(res, JobResult):
        if res.ok:
            logger.info("定时任务完成：%s %s", name, res.detail)
        else:
            logger.warning("定时任务失败：%s %s", name, res.detail)
    return res


def _wrap(name: str, fn: Callable) -> Callable:
    """包一层日志。

    APScheduler 的 AsyncIOExecutor 用 iscoroutinefunction 决定「create_task 直接跑」
    还是「丢线程池」；包装器必须保持协程性，否则 nightly_review 会被丢进线程池
    返回一个没人 await 的 coroutine。
    """
    if inspect.iscoroutinefunction(fn):

        async def awrapper(*a, **kw):
            try:
                return _report(name, await fn(*a, **kw))
            except Exception:
                logger.exception("定时任务异常：%s", name)
                return None

        return awrapper

    def wrapper(*a, **kw):
        try:
            return _report(name, fn(*a, **kw))
        except Exception:
            logger.exception("定时任务异常：%s", name)
            return None

    return wrapper


def build_scheduler(ctx):
    """构建 APScheduler（若可用）。返回 (scheduler, 描述)。"""
    try:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        from apscheduler.triggers.cron import CronTrigger
    except Exception:
        return None, {"available": False, "reason": "APScheduler 未安装"}

    cfg = load_scheduler_config(ctx.workbench.paths.config_dir)
    if not cfg.get("enabled"):
        return None, {"available": True, "enabled": False, "config": cfg}

    runner = JobRunner(ctx)
    sched = AsyncIOScheduler(timezone="Asia/Shanghai")
    registered: list[dict[str, Any]] = []

    jobs: dict[str, Callable] = {
        "nightly_review": runner.nightly_review,
        "index_rebuild": runner.index_rebuild,
        "backup": runner.backup,
    }
    raw_jobs = cfg.get("jobs")
    if not isinstance(raw_jobs, dict):
        logger.warning("scheduler.yaml 的 jobs 段不是键值表（%s），已忽略", type(raw_jobs).__name__)
        raw_jobs = {}
    for name, spec in raw_jobs.items():
        if not isinstance(spec, dict):
            logger.warning("定时任务 %s 的配置不是键值表（%s），已忽略", name, type(spec).__name__)
            continue
        if not spec.get("enabled"):
            continue
        fn = jobs.get(name)
        if fn is None:
            continue
        try:
            trigger = CronTrigger.from_crontab(spec.get("cron", "0 3 * * *"), timezone="Asia/Shanghai")
            sched.add_job(_wrap(name, fn), trigger, id=name, replace_existing=True, misfire_grace_time=3600)
            registered.append({"job": name, "cron": spec.get("cron"), "note": spec.get("note", "")})
        except Exception as exc:
            logger.exception("注册定时任务失败：%s", name)
            registered.append({"job": name, "error": str(exc)})

    return sched, {"available": True, "enabled": True, "jobs": registered, "config": cfg}
