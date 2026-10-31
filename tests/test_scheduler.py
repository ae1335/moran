"""定时任务（§14.1）：三个 job 的可独立调用路径 + 真实 cron 触发。

为什么这些测试存在：docs/PROJECT.md §7.6 承诺「定时任务失败 -> 记日志，不阻断服务」，
§14.1 承诺夜审/索引/备份能定时跑。此前只有一条断言「默认 available=True、enabled=False」
的测试，等于三个 job 从来没被执行过——探针一跑就炸出三个缺陷（同秒备份必失败、
失败完全无日志、DEFAULTS 被共享出去）。
"""
from __future__ import annotations

import asyncio
import inspect
import logging
from pathlib import Path

import pytest

from server.api.deps import AppContext
from server.model.base import RouteHop, RoutingTable
from server.model.providers.mock import MockProvider
from server.model.providers.registry import ProviderRegistry, ProviderSpec
from server.core.schema import BookMeta, ChapterOutline
from server.stats.scheduler import (
    DEFAULTS,
    JobRunner,
    _unique_dir,
    _wrap,
    build_scheduler,
    load_scheduler_config,
)


# ---------------------------------------------------------------- 夹具


def _mock_only(ctx: AppContext) -> None:
    """把 gateway 换成纯 mock —— 测试必须离线确定性。

    `ctx` 夹具建的是 tmp 目录下的 ws，那里没有 routing.yaml，于是走
    `providers/registry.py` 的内置默认路由：review → workbuddy-local(kimi-k2.7)
    → deepseek-official(deepseek-chat)。而 `JobRunner.nightly_review` 会用
    `use_model=True` 真去连外网。本机无凭据时单次连接就挂在 httpcore 的
    connect_tcp 上（探针实测 >90s 仍不返回），20s 的 cron deadline 必超时 ——
    `test_real_cron_fires_and_logs` 因此变成一条「取决于这台机器能不能连上
    deepseek」的测试：联网就绿，不联网就红。这是假绿的一种，与
    `tests/test_api.py::_patch_gateway` 用的是同一套做法。
    """
    reg = ProviderRegistry()
    reg.providers["mock-local"] = MockProvider(name="mock-local")
    reg.specs["mock-local"] = ProviderSpec(name="mock-local", kind="mock", models=["mock-1"])
    table = RoutingTable(
        routes={
            k: [RouteHop("mock-local", "mock-1")]
            for k in ("plan", "write", "review", "curator", "polish", "summarize", "deconstruct", "free")
        }
    )
    ctx.gateway.registry = reg
    ctx.gateway.routing = table
    ctx.invalidate()


@pytest.fixture()
def ctx(tmp_path: Path) -> AppContext:
    c = AppContext.create(tmp_path / "ws")
    c.workbench.create_novel(BookMeta(title="定时", platform="tomato"))
    _mock_only(c)
    return c


def _book_dir(ctx: AppContext) -> str:
    return ctx.workbench.refresh_shelf().novels[0].dir


def _write_drafted(ctx: AppContext, ch: int = 1) -> str:
    """写一章正文 + 章纲，状态 drafted（夜审的唯一目标）。"""
    d = _book_dir(ctx)
    bc = ctx.find_book(d)
    bc.store.write_chapter(ch, f"第{ch}章 正文内容，至少几个字。")
    bc.store.save_outline(ChapterOutline(ch=ch, title="试", status="drafted"))
    return d


def _messages(caplog) -> str:
    """caplog 里每条已经是格式化好的消息，不要再套 %。"""
    return "\n".join(rec.getMessage() for rec in caplog.records)


# ---------------------------------------------------------------- 配置


def test_config_missing_file_returns_isolated_copy(tmp_path: Path):
    """缺 scheduler.yaml 时必须返回副本。

    直接返回模块级 DEFAULTS 会把内层 jobs 字典的引用漏出去，
    调用方一次就地修改就能污染之后所有请求。
    """
    cfg = load_scheduler_config(tmp_path)
    assert cfg == DEFAULTS
    assert cfg is not DEFAULTS
    assert cfg["jobs"] is not DEFAULTS["jobs"]

    cfg["enabled"] = True
    cfg["jobs"]["backup"]["enabled"] = True
    assert DEFAULTS["enabled"] is False
    assert DEFAULTS["jobs"]["backup"]["enabled"] is False


def test_config_file_overrides_defaults(tmp_path: Path):
    (tmp_path / "scheduler.yaml").write_text(
        "enabled: true\njobs:\n  backup:\n    enabled: true\n", encoding="utf-8"
    )
    cfg = load_scheduler_config(tmp_path)
    assert cfg["enabled"] is True
    assert cfg["jobs"]["backup"]["enabled"] is True
    # 没写的 job 仍保留默认值
    assert cfg["jobs"]["nightly_review"]["enabled"] is False
    assert DEFAULTS["enabled"] is False


# ---------------------------------------------------------------- 夜审


async def test_nightly_review_upgrades_status(ctx: AppContext):
    d = _write_drafted(ctx, 1)
    r = await JobRunner(ctx).nightly_review()

    assert r.job == "nightly_review"
    assert r.ok is True
    assert [row["ch"] for row in r.data[d]] == [1]
    assert "error" not in r.data[d][0]
    # reviewer 只在 status in (drafted, writing) 时升 reviewed
    assert ctx.find_book(d).store.load_outline(1).status == "reviewed"


async def test_nightly_review_skips_non_drafted(ctx: AppContext):
    d = _book_dir(ctx)
    bc = ctx.find_book(d)
    bc.store.write_chapter(1, "第一章正文内容。")
    bc.store.save_outline(ChapterOutline(ch=1, title="已定稿", status="final"))

    r = await JobRunner(ctx).nightly_review()
    assert r.ok is True
    assert r.data[d] == []


class _BoomReviewer:
    async def review(self, ch, **kw):
        raise RuntimeError("模型挂了")


async def test_nightly_review_failure_is_logged_not_raised(
    ctx: AppContext, monkeypatch, caplog
):
    """单章失败不阻断其它章，且必须留日志（§7.6 失败不静默）。"""
    d = _write_drafted(ctx, 1)
    _write_drafted(ctx, 2)
    monkeypatch.setattr(
        type(ctx.find_book(d)), "reviewer", property(lambda self: _BoomReviewer())
    )

    with caplog.at_level(logging.ERROR, logger="workbench.scheduler"):
        r = await JobRunner(ctx).nightly_review()

    assert r.ok is False
    assert len(r.data[d]) == 2
    assert all("error" in row for row in r.data[d])
    logged = _messages(caplog)
    assert "夜审失败" in logged
    assert "第 1 章" in logged and "第 2 章" in logged


# ---------------------------------------------------------------- 索引


def test_index_rebuild_reports_verify(ctx: AppContext):
    d = _write_drafted(ctx, 1)
    ctx.find_book(d).retrieval.build_index(full=True)

    r = JobRunner(ctx).index_rebuild()
    assert r.job == "index_rebuild"
    assert r.ok is True
    assert r.data[d]["verify"]["ok"] is True
    # 刚全量建过，增量这次没有可改的章
    assert r.data[d]["build"]["changed"] == []


def test_index_rebuild_heals_drift(ctx: AppContext):
    """正文改了没重建索引 -> 夜间索引任务应当把它重新收进索引并归零。"""
    d = _write_drafted(ctx, 1)
    bc = ctx.find_book(d)
    bc.retrieval.build_index(full=True)
    assert bc.retrieval.verify()["ok"] is True

    bc.store.write_chapter(1, "第1章 正文被改过了，hash 必然不一致，这次多写一点。")
    assert bc.retrieval.verify()["drifted"] == [1]

    r = JobRunner(ctx).index_rebuild()
    assert r.ok is True
    assert r.data[d]["build"]["changed"] == [1]
    assert r.data[d]["verify"]["ok"] is True
    assert bc.retrieval.verify()["drifted"] == []


def test_index_rebuild_failure_is_logged(ctx: AppContext, monkeypatch, caplog):
    d = _write_drafted(ctx, 1)
    bc = ctx.find_book(d)

    def boom(*a, **kw):
        raise RuntimeError("索引库锁住了")

    monkeypatch.setattr(bc.retrieval, "build_index", boom)

    with caplog.at_level(logging.ERROR, logger="workbench.scheduler"):
        r = JobRunner(ctx).index_rebuild()

    assert r.ok is False
    assert "RuntimeError" in r.data[d]["error"]
    assert "索引重建失败" in _messages(caplog)


# ---------------------------------------------------------------- 备份


def test_backup_twice_in_same_second(ctx: AppContext):
    """同一秒内连跑两次备份：修复前第二次必然 FileExistsError。"""
    _write_drafted(ctx, 1)
    runner = JobRunner(ctx)
    first = runner.backup()
    second = runner.backup()

    assert first.ok is True, first.detail
    assert second.ok is True, second.detail
    assert first.data["path"] != second.data["path"]
    assert (Path(first.data["path"]) / "novels").is_dir()
    assert (Path(second.data["path"]) / "novels").is_dir()


def test_unique_dir_appends_suffix(tmp_path: Path):
    (tmp_path / "20260101-000000").mkdir()
    assert _unique_dir(tmp_path, "20260101-000000") == tmp_path / "20260101-000000-2"
    (tmp_path / "20260101-000000-2").mkdir()
    assert _unique_dir(tmp_path, "20260101-000000") == tmp_path / "20260101-000000-3"


def test_backup_keeps_only_n(ctx: AppContext):
    """backup_keep 生效：只保留最近 N 份。"""
    _write_drafted(ctx, 1)
    cfg_dir = ctx.workbench.paths.config_dir
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "scheduler.yaml").write_text("backup_keep: 2\n", encoding="utf-8")

    runner = JobRunner(ctx)
    for _ in range(4):
        assert runner.backup().ok is True

    kept = sorted(p.name for p in (ctx.root / "backups").iterdir() if p.is_dir())
    assert len(kept) == 2


def test_backup_failure_logged_and_no_leftover(ctx: AppContext, monkeypatch, caplog):
    """copytree 炸了：返回 ok=False、留日志、不留半成品目录。"""

    def boom(*a, **kw):
        raise OSError("磁盘满了")

    monkeypatch.setattr("server.stats.scheduler.shutil.copytree", boom)
    _write_drafted(ctx, 1)

    with caplog.at_level(logging.ERROR, logger="workbench.scheduler"):
        r = JobRunner(ctx).backup()

    assert r.ok is False
    assert "OSError" in r.detail
    assert (ctx.root / "backups").is_dir()
    assert list((ctx.root / "backups").iterdir()) == []
    assert "备份失败" in _messages(caplog)


# ---------------------------------------------------------------- 装配


def _shutdown(sched) -> None:
    """没 start 过的 scheduler 调 shutdown 会抛 SchedulerNotRunningError。"""
    if sched is not None and sched.running:
        sched.shutdown(wait=False)


def test_build_scheduler_disabled_by_default(ctx: AppContext):
    sched, info = build_scheduler(ctx)
    assert sched is None
    assert info["available"] is True
    assert info["enabled"] is False


def test_build_scheduler_registers_enabled_jobs(ctx: AppContext):
    cfg_dir = ctx.workbench.paths.config_dir
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "scheduler.yaml").write_text(
        "enabled: true\njobs:\n  backup:\n    enabled: true\n"
        "  nightly_review:\n    enabled: true\n    cron: '0 3 * * *'\n"
        "  index_rebuild:\n    enabled: false\n",
        encoding="utf-8",
    )
    sched, info = build_scheduler(ctx)
    try:
        assert info["enabled"] is True
        assert {row["job"] for row in info["jobs"]} == {"backup", "nightly_review"}
        assert {j.id for j in sched.get_jobs()} == {"backup", "nightly_review"}
        # 包装器必须保持协程性，否则 AsyncIOExecutor 会把它丢进线程池，
        # nightly_review 就变成没人 await 的 coroutine。
        assert inspect.iscoroutinefunction(sched.get_job("nightly_review").func)
        assert not inspect.iscoroutinefunction(sched.get_job("backup").func)
    finally:
        _shutdown(sched)


def test_build_scheduler_bad_cron_is_reported_not_raised(ctx: AppContext, caplog):
    cfg_dir = ctx.workbench.paths.config_dir
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "scheduler.yaml").write_text(
        "enabled: true\njobs:\n  backup:\n    enabled: true\n    cron: 'not a cron'\n",
        encoding="utf-8",
    )
    with caplog.at_level(logging.ERROR, logger="workbench.scheduler"):
        sched, info = build_scheduler(ctx)
    try:
        assert info["enabled"] is True
        assert info["jobs"][0]["job"] == "backup"
        assert "error" in info["jobs"][0]
        assert sched.get_jobs() == []
        assert "注册定时任务失败" in _messages(caplog)
    finally:
        _shutdown(sched)


# ---------------------------------------------------------------- 真实 cron 触发


async def test_real_cron_fires_and_logs(ctx: AppContext, caplog):
    """真的等一次 cron 触发——这是「定时任务实跑」的证据本体。

    用每秒触发（CronTrigger(second="*")）+ 短轮询，验证 APScheduler 真调到了
    JobRunner、任务真的改了章纲状态、成功也留了日志（否则 cron 侧完全静默）。
    """
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger

    d = _write_drafted(ctx, 1)
    runner = JobRunner(ctx)
    sched = AsyncIOScheduler(timezone="Asia/Shanghai")
    sched.add_job(
        _wrap("nightly_review", runner.nightly_review),
        CronTrigger(second="*"),
        id="nightly_review",
    )

    with caplog.at_level(logging.INFO, logger="workbench.scheduler"):
        sched.start()
        try:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 20.0
            while loop.time() < deadline:
                if ctx.find_book(d).store.load_outline(1).status == "reviewed":
                    break
                await asyncio.sleep(0.2)
        finally:
            sched.shutdown(wait=False)

    assert ctx.find_book(d).store.load_outline(1).status == "reviewed"
    assert "定时任务完成" in _messages(caplog)

# ---------------------------------------------------------------- 应用装配（缺陷回归）


def test_app_boots_with_scheduler_enabled(tmp_path: Path):
    """启用定时任务后 create_app 必须能起来，且 lifespan 真的 start/shutdown。

    回归的缺陷：server/app.py 旧写法 app.add_event_handler("startup", ...) 在
    fastapi 0.162 里已不存在 -> AttributeError: 'FastAPI' object has no attribute
    'add_event_handler'。后果是**只要 scheduler.yaml 写 enabled: true，服务直接起不来**。
    这条路径长期没人实跑：默认配置恰好把 enabled 关掉，测试也只断言了「默认关」。
    """
    from fastapi.testclient import TestClient

    import server.app as appmod

    ws = tmp_path / "ws"
    c = AppContext.create(ws)
    c.workbench.create_novel(BookMeta(title="启动", platform="tomato"))
    cfg = ws / "workbench-config"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "scheduler.yaml").write_text(
        "enabled: true\njobs:\n  backup:\n    enabled: true\n    cron: '* * * * *'\n",
        encoding="utf-8",
    )

    captured: dict[str, object] = {}
    original = appmod._try_build_scheduler

    def _spy(ctx):
        sched, info = original(ctx)
        captured["sched"] = sched
        return sched, info

    appmod._try_build_scheduler = _spy
    try:
        app = appmod.create_app(ws)
    finally:
        appmod._try_build_scheduler = original

    sched = captured["sched"]
    assert sched is not None, "enabled: true 时必须真的建出 scheduler"
    assert getattr(sched, "running", None) is False, "构造阶段不该已经启动"

    with TestClient(app) as cli:
        info = cli.get("/api/jobs/scheduler").json()
        assert info["available"] is True
        assert info["enabled"] is True
        assert [j["job"] for j in info["jobs"]] == ["backup"]
        assert getattr(sched, "running", None) is True, "lifespan 启动时 scheduler 必须 start"
        assert [j.id for j in sched.get_jobs()] == ["backup"]

    assert getattr(sched, "running", None) is False, "lifespan 退出时必须 shutdown"


# ------------------------------------------------- 缺陷 #92：备份保留会删掉作者自己的文件夹


def test_backup_retention_only_prunes_its_own_dirs(tmp_path: Path):
    """`JobRunner.backup` 的「只留最近 N 份」不能删备份区里的其它目录。

    修复前 `existing = sorted(dest_root.iterdir())` 把备份区里**所有**目录都当成旧备份：
    作者自己在 backups/ 下放一个 `0-我的重要资料`（字典序排在所有时间戳前面），下一次
    备份就会被 `shutil.rmtree` 直接删掉——备份任务变成了删数据任务。
    现在只认「我们自己写的」：目录名匹配 `YYYYmmdd-HHMMSS`（含 -2/-3 变体）且里面有 novels/。
    """
    import json

    from server.api.deps import AppContext
    from server.core.schema import BookMeta
    from server.stats.scheduler import JobRunner

    ws = tmp_path / "ws"
    ctx = AppContext.create(ws)
    st = ctx.workbench.create_novel(BookMeta(title="保留清理", platform="tomato"))
    st.write_chapter(1, "第一章正文内容。" * 20)
    cfgdir = ctx.workbench.paths.config_dir
    cfgdir.mkdir(parents=True, exist_ok=True)
    (cfgdir / "scheduler.yaml").write_text(
        json.dumps({"enabled": True, "backup_dir": "backups", "backup_keep": 2}),
        encoding="utf-8",
    )

    bak = ws / "backups"
    bak.mkdir(parents=True, exist_ok=True)
    mine = bak / "0-我的重要资料"
    mine.mkdir()
    (mine / "note.txt").write_text("作者自己放的东西", encoding="utf-8")
    also_mine = bak / "随便一个名字"
    also_mine.mkdir()

    jr = JobRunner(ctx)
    for _ in range(3):
        r = jr.backup()
        assert r.ok, r.detail

    names = sorted(p.name for p in bak.iterdir())
    assert "0-我的重要资料" in names, names
    assert "随便一个名字" in names, names
    assert (mine / "note.txt").read_text(encoding="utf-8") == "作者自己放的东西"
    # 真正的备份只留最近 2 份
    import re
    ours = [n for n in names if re.match(r"^\d{8}-\d{6}(-\d+)?$", n)]
    assert len(ours) == 2, names


# 缺陷 #98：定时任务 job 抛异常 / jobs 段不是键值表时不能静默
def test_job_wrapper_logs_unexpected_exceptions(caplog):
    """cron 触发的 job 抛异常时必须 logger.exception（§7.6「失败不静默降级」）。

    三个 job 内部都有 try/except，但注册表之外的意外（比如磁盘被拔、库被换）
    会从包装器里冒出去；没人 await 返回值，不记日志就彻底无声。
    """
    import logging

    async def boom():
        raise RuntimeError("磁盘没了")

    def boom_sync():
        raise RuntimeError("磁盘没了")

    with caplog.at_level(logging.ERROR, logger="workbench.scheduler"):
        import asyncio

        assert asyncio.run(_wrap("x", boom)()) is None
        assert _wrap("y", boom_sync)() is None
    msgs = "".join(r.getMessage() for r in caplog.records)
    assert "定时任务异常" in msgs


def test_jobs_section_shape_is_defended(tmp_path):
    """jobs 段被写成列表 / 条目被写成标量时不能让 build_scheduler 崩。"""
    import yaml

    from server.api.deps import AppContext

    ws = tmp_path / "ws"
    AppContext.create(ws)
    cfgdir = ws / "workbench-config"
    cfgdir.mkdir(parents=True, exist_ok=True)
    (cfgdir / "scheduler.yaml").write_text("enabled: true\njobs:\n  - a\n  - b\n", encoding="utf-8")
    with pytest.raises(ValueError) as ei:
        load_scheduler_config(ws / "workbench-config")
    assert "jobs" in str(ei.value)

    # jobs 段是键值表但条目是标量
    (cfgdir / "scheduler.yaml").write_text("enabled: true\njobs:\n  backup: oops\n", encoding="utf-8")
    sched2, info2 = build_scheduler(AppContext.create(ws))
    assert sched2 is not None
    assert sched2.get_jobs() == []  # 条目不是键值表 → 跳过，不注册


def test_scheduler_start_failure_is_visible_in_status(tmp_path: Path):
    """缺陷 #120：调度器 start() 挂了，「已开启」是假的。

    lifespan 里 ``sched.start()`` 的异常被静默 pass，服务照常起来，而
    ``/api/jobs/scheduler`` 只回 ``available`` / ``enabled`` —— 这两个字段在
    start 失败时**照样是 true**。作者开了夜审以为每晚 3 点会跑，实际一次都不会
    触发，且没有任何地方能看出来。与 #45（一开就崩）是同一条链上的两端：
    #45 是「崩得看得见」，#120 是「没崩但也没在跑，且看不见」。
    """
    from fastapi.testclient import TestClient

    import server.app as appmod

    ws = tmp_path / "ws120"
    c = AppContext.create(ws)
    c.workbench.create_novel(BookMeta(title="启停", platform="tomato"))
    cfg = ws / "workbench-config"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "scheduler.yaml").write_text(
        "enabled: true\njobs:\n  backup:\n    enabled: true\n    cron: '* * * * *'\n",
        encoding="utf-8",
    )

    captured: dict[str, object] = {}
    original = appmod._try_build_scheduler

    class BoomScheduler:
        """包住真 scheduler，只让 start() 炸。"""

        def __init__(self, inner):
            self._i = inner
            self.running = False

        def start(self):
            raise RuntimeError("事件循环冲突，调度器起不来")

        def shutdown(self, wait=False):
            pass

        def get_jobs(self):
            return []

    def _spy(ctx):
        sched, info = original(ctx)
        boom = BoomScheduler(sched)
        captured["sched"] = boom
        return boom, info

    appmod._try_build_scheduler = _spy
    try:
        app = appmod.create_app(ws)
    finally:
        appmod._try_build_scheduler = original

    with TestClient(app) as cli:
        r = cli.get("/api/jobs/scheduler").json()
    # 服务不能因为调度器起不来就挂掉
    assert r.get("available") is True
    assert r.get("enabled") is True
    # 但「开了却没在跑」必须看得见
    assert r.get("running") is False, "start() 失败了却报 running=True"
    assert "事件循环冲突" in (r.get("error") or ""), r
    warn = r.get("warning") or ""
    assert "没在跑" in warn and "不会执行" in warn, warn
