"""FastAPI 应用装配。

启动：uvicorn server.app:app --host 127.0.0.1 --port 8765

**路由注册顺序很关键**（Starlette 按注册顺序匹配）：
  1. /api/*            API 路由（最先，任何 catch-all 都抢不走）
  2. /api/jobs/scheduler 定时任务状态
  3. /pwa              PWA 静态挂载
  4. /assets           前端构建产物静态
  5. /                 SPA 入口
  6. /{full_path:path} SPA fallback —— 必须显式拒绝 /api 前缀，
     否则未知 API 路径会返回 200 HTML，前端把它当 JSON 解析后报出与真实原因无关的错。
"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .api.deps import AppContext, set_context

logger = logging.getLogger("workbench.app")
from .api.routes import router


def _try_build_scheduler(ctx: AppContext) -> tuple[Any, dict[str, Any]]:
    """定时任务永远不该阻断主服务：任何异常都降级为「不可用」。"""
    try:
        from .stats.scheduler import build_scheduler

        return build_scheduler(ctx)
    except Exception as exc:  # pragma: no cover
        return None, {"available": False, "error": f"{type(exc).__name__}: {exc}"}


def create_app(root: Path | str | None = None) -> FastAPI:
    ctx = AppContext.create(root)
    set_context(ctx)

    # 定时任务默认关闭；scheduler.yaml 显式开启才真的跑。
    # 必须在构造 FastAPI 之前建好：lifespan 要作为构造参数传进去，
    # 而 FastAPI 0.162 已移除 app.add_event_handler（旧写法在这里直接 AttributeError，
    # 导致「一开定时任务服务就起不来」，属于从来没被实跑过的路径）。
    sched, sched_info = _try_build_scheduler(ctx)

    #: 缺陷 #120：调度器 start() 失败时，「已开启」是假的。作者开了夜审以为每晚
    #: 会跑，实际一次都不会触发，而 /api/jobs/scheduler 依旧报 available/enabled
    #: 全绿。这里把失败记下来，由状态端点如实回传。
    sched_state: dict[str, Any] = {"running": False, "error": ""}

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if sched is not None:
            try:
                sched.start()
                sched_state["running"] = True
                logger.info("定时任务调度器已启动：%s", 
                            ", ".join(j.id for j in sched.get_jobs()) or "无任务")
            except Exception as exc:  # 启动失败不阻断主服务
                sched_state["error"] = f"{type(exc).__name__}: {exc}"
                logger.exception("定时任务调度器启动失败，定时任务不会执行")
        try:
            yield
        finally:
            if sched is not None:
                try:
                    if getattr(sched, "running", False):
                        sched.shutdown(wait=False)
                except Exception:  # pragma: no cover - 关停失败不阻断退出
                    pass

    app = FastAPI(
        title="网文工作台",
        version="1.0.0",
        description="本地优先、文件即真相源的长篇网文创作工作台",
        lifespan=lifespan,
    )
    app.state.ctx = ctx

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://127.0.0.1:5173",
            "http://localhost:5173",
            "http://127.0.0.1:8765",
            "http://localhost:8765",
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ---- 1) API（最先注册）----
    app.include_router(router)

    # ---- 定时任务状态（调度器已在构造函数前建好，见 lifespan）----
    app.state.scheduler_info = sched_info

    @app.get("/api/jobs/scheduler", tags=["jobs"])
    def scheduler_info() -> dict[str, Any]:
        info = dict(getattr(app.state, "scheduler_info", {"available": False}))
        # 缺陷 #120：光有 available/enabled 不够——start() 挂了这两个字段照样是
        # true。把真实运行状态并进去，让「开了却没在跑」能被看见。
        info["running"] = bool(sched_state["running"])
        info["error"] = str(sched_state["error"])
        if info.get("available") and info.get("enabled") and not info["running"]:
            info["warning"] = (
                "定时任务已开启但调度器没在跑（%s），任务不会执行"
                % (info["error"] or "未知原因")
            )
        return info

    here = Path(__file__).resolve().parent.parent

    # ---- 2) PWA（不依赖前端构建）----
    pwa_dir = here / "pwa"
    if pwa_dir.exists():
        app.mount("/pwa", StaticFiles(directory=str(pwa_dir), html=True), name="pwa")

    # ---- 3) 前端构建产物 ----
    web_dist = here / "web" / "dist"
    if web_dist.exists():
        assets = web_dist / "assets"
        if assets.exists():
            app.mount("/assets", StaticFiles(directory=str(assets)), name="assets")

        @app.get("/", include_in_schema=False)
        def index() -> FileResponse:
            return FileResponse(str(web_dist / "index.html"))

        @app.get("/favicon.ico", include_in_schema=False)
        def favicon():
            # 只构建了 favicon.svg 时回退到 svg：浏览器无 <link rel=icon> 时
            # 仍会自动请求 /favicon.ico，直接 404 会在控制台留下每次必现的报错。
            for name, mime in (("favicon.ico", None), ("favicon.svg", "image/svg+xml")):
                f = web_dist / name
                if f.exists():
                    return FileResponse(str(f), media_type=mime) if mime else FileResponse(str(f))
            raise HTTPException(404)

        @app.get("/{full_path:path}", include_in_schema=False)
        def spa(full_path: str):
            # 绝不吞 API：未知 /api 路径必须 404，不能返回 HTML
            if full_path.startswith("api/") or full_path == "api":
                raise HTTPException(404, f"未知 API 路径: /{full_path}")
            target = (web_dist / full_path).resolve()
            try:
                inside = target == web_dist.resolve() or web_dist.resolve() in target.parents
            except OSError:
                inside = False
            if inside and target.is_file():
                return FileResponse(str(target))
            return FileResponse(str(web_dist / "index.html"))

    else:

        @app.get("/", include_in_schema=False)
        def fallback_index() -> JSONResponse:
            return JSONResponse(
                {
                    "message": "后端已启动。前端未构建：cd web && npm install && npm run build",
                    "pwa": "/pwa/index.html",
                    "api_docs": "/docs",
                }
            )

        @app.get("/{full_path:path}", include_in_schema=False)
        def fallback_any(full_path: str):
            if full_path.startswith("api/") or full_path == "api":
                raise HTTPException(404, f"未知 API 路径: /{full_path}")
            raise HTTPException(404, f"未找到: /{full_path}（前端未构建：cd web && npm run build）")

    @app.exception_handler(RequestValidationError)
    async def bad_body(request, exc):
        """pydantic 校验失败 -> 400（缺陷 #68）。

        FastAPI 默认给 422，但这个服务的所有入站错误契约都是 400，
        且 #68 的通用上界就是靠 pydantic 校验阶段实现的——不统一状态码，
        「上界被触发」和「字段类型填错」会分成两种状态码，前端要处理两套。
        """
        first = exc.errors()[0] if exc.errors() else {}
        where = ".".join(str(q) for q in (first.get("loc") or [])[1:]) or "body"
        msg = first.get("msg") or "载荷不合法"
        logger.warning("载荷不合法：%s %s -> %s %s", request.method, request.url.path, where, msg)
        return JSONResponse(status_code=400, content={"detail": f"{where} {msg}"})

    @app.exception_handler(ValueError)

    async def bad_value(request, exc):
        """参数类 ValueError -> 400。

        缺陷 #66：章号/书名/区间这些守卫抛的是 ValueError，而端点里只有 3 处
        （10 个 except 中的 3 个）手动转成 400/404，其余全部漏到 §65 的兜底
        Exception handler，返回 500。实测 `PUT /chapters/1000000000` 返回
        `500 {"error":"ValueError: 章号超出范围..."}`——客户端的毛病变成了服务端的错。
        只兜 ValueError（参数问题），不碰 KeyError/TypeError（那是代码 bug，该 500）。
        """
        logger.warning("参数不合法：%s %s -> %s", request.method, request.url.path, exc)
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.exception_handler(Exception)
    async def unhandled(request, exc):

        """未捕获异常统一 500。

        缺陷 #65：这里原先既不打日志也不留 traceback，生产上就是一个黑盒——
        报 500 了但服务端什么都没留下，事后根本没法追。现在走 logger.exception
        （不阻断：仍然返回 500，且响应体不含 traceback，不泄露内部路径）。
        """
        logger.exception(
            "未捕获异常：%s %s -> %s: %s", request.method, request.url.path, type(exc).__name__, exc
        )
        return JSONResponse(status_code=500, content={"error": f"{type(exc).__name__}: {exc}"})

    return app


app = create_app(os.environ.get("NOVEL_WORKBENCH_ROOT"))
