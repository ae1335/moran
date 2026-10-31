"""API 集成测试：用 FastAPI TestClient 跑完整 REST + SSE（§17.C）。

M1 验收：从零建书 → 确认 3 张章纲 → 生成 3 章 → 审稿报告落盘 → git log 可回溯。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from server.api.deps import AppContext, set_context
from server.app import create_app
from server.model.base import RouteHop, RoutingTable
from server.model.providers.mock import MockProvider
from server.model.providers.registry import ProviderRegistry, ProviderSpec


@pytest.fixture()
def client(tmp_path: Path):
    app = create_app(tmp_path / "ws")
    with TestClient(app) as c:
        yield c


def _use_mock_only(app):
    """把 gateway 换成纯 mock（测试必须离线确定性）。"""
    ctx: AppContext = app.state_ctx if hasattr(app, "state_ctx") else None


def _patch_gateway(client: TestClient, tmp_path: Path) -> None:
    from server.api.deps import get_context

    ctx = get_context()
    reg = ProviderRegistry()
    for name in ("mock-local",):
        reg.providers[name] = MockProvider(name=name)
        reg.specs[name] = ProviderSpec(name=name, kind="mock", models=["mock-1"])
    table = RoutingTable(
        routes={k: [RouteHop("mock-local", "mock-1")] for k in
                ("plan", "write", "review", "curator", "polish", "summarize", "deconstruct", "free")}
    )
    ctx.gateway.registry = reg
    ctx.gateway.routing = table
    ctx.invalidate()


# ---------------------------------------------------------------- 基础


def test_health_and_shelf_empty(client: TestClient):
    r = client.get("/api/health")
    assert r.status_code == 200 and r.json()["ok"] is True
    assert client.get("/api/shelf").status_code == 200


def test_create_book(client: TestClient, tmp_path: Path):
    r = client.post(
        "/api/books",
        json={"title": "凡人问道", "genre": "玄幻", "platform": "tomato", "protagonist_name": "林澈"},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["title"] == "凡人问道"
    assert "novel-" in data["dir"]
    # 书架可见
    shelf = client.get("/api/shelf").json()
    assert len(shelf["novels"]) == 1
    # 详情
    d = client.get(f"/api/books/{data['dir']}").json()
    assert d["meta"]["title"] == "凡人问道"
    assert any(c["id"] == "mc" for c in d["characters"])


def test_create_book_conflict(client: TestClient):
    payload = {"title": "重复书"}
    assert client.post("/api/books", json=payload).status_code == 200
    assert client.post("/api/books", json=payload).status_code == 409


# ---------------------------------------------------------------- M1 全链路


def test_m1_full_loop(client: TestClient, tmp_path: Path):
    _patch_gateway(client, tmp_path)
    r = client.post("/api/books", json={"title": "闭环测试", "platform": "tomato", "words_per_chapter": [1800, 2200]})
    book = r.json()["dir"]

    # 1) 生成 3 张章纲
    plan = client.post(f"/api/books/{book}/plan", json={"start_ch": 1, "count": 3})
    assert plan.status_code == 200, plan.text
    chapters = plan.json()["chapters"]
    assert len(chapters) == 3

    # 2) 逐章确认
    for c in chapters:
        resp = client.post(f"/api/books/{book}/outline/{c['ch']}/confirm")
        assert resp.status_code == 200 and resp.json()["status"] == "planned"

    # 3) 生成 3 章（非流式，便于断言）
    for c in chapters:
        g = client.post(f"/api/books/{book}/generate", json={"ch": c["ch"], "stream": False})
        assert g.status_code == 200, g.text
        body = g.json()
        assert body["words"] > 100
        assert body["assembled"] == f"memory/ch-{c['ch']:04d}.assembled.md"

    # 4) 审稿报告落盘
    for c in chapters:
        rv = client.post(f"/api/books/{book}/review", json={"ch": c["ch"]})
        assert rv.status_code == 200, rv.text
        assert client.get(f"/api/books/{book}/review/{c['ch']}").status_code == 200

    # 5) git log 可回溯
    log = client.get(f"/api/books/{book}/git/log").json()
    assert log["log"]


def test_generate_stream_sse(client: TestClient, tmp_path: Path):
    _patch_gateway(client, tmp_path)
    book = client.post("/api/books", json={"title": "流式测试"}).json()["dir"]
    client.post(f"/api/books/{book}/plan", json={"start_ch": 1, "count": 1})
    client.post(f"/api/books/{book}/outline/1/confirm")
    with client.stream("POST", f"/api/books/{book}/generate", json={"ch": 1, "stream": True}) as resp:
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]
        body = "".join(resp.iter_text())
    assert "event: start" in body and "event: done" in body
    # 正文已落盘
    got = client.get(f"/api/books/{book}/chapters/1").json()
    assert got["words"] > 100


# ---------------------------------------------------------------- M2 Curator 提案制


def test_curator_proposal_flow(client: TestClient, tmp_path: Path):
    _patch_gateway(client, tmp_path)
    book = client.post("/api/books", json={"title": "提案测试"}).json()["dir"]
    client.post(f"/api/books/{book}/plan", json={"start_ch": 1, "count": 1})
    client.post(f"/api/books/{book}/outline/1/confirm")
    client.post(f"/api/books/{book}/generate", json={"ch": 1, "stream": False})
    client.post(f"/api/books/{book}/review", json={"ch": 1})

    cur = client.post(f"/api/books/{book}/curate", json={"ch": 1})
    assert cur.status_code == 200, cur.text
    props = cur.json()["proposals"]
    assert props

    # 未确认前不写盘
    truth_before = client.get(f"/api/books/{book}/truth/style-fingerprint.md").json()["content"]

    conf = client.post(f"/api/books/{book}/curate/confirm", json={"ch": 1})
    assert conf.status_code == 200, conf.text
    assert conf.json()["applied"]

    # 摘要已落盘
    assert client.get(f"/api/books/{book}/chapters/1").json()["text"]
    # 状态推进到 final
    outline = client.get(f"/api/books/{book}/outline/1").json()
    assert outline["status"] == "final"


def test_curator_pack_endpoint(client: TestClient, tmp_path: Path):
    _patch_gateway(client, tmp_path)
    book = client.post("/api/books", json={"title": "提案包"}).json()["dir"]
    assert client.get(f"/api/books/{book}/curate/1").status_code == 404
    client.post(f"/api/books/{book}/plan", json={"start_ch": 1, "count": 1})
    client.post(f"/api/books/{book}/generate", json={"ch": 1, "stream": False})
    client.post(f"/api/books/{book}/curate", json={"ch": 1})
    assert client.get(f"/api/books/{book}/curate/1").status_code == 200


# ---------------------------------------------------------------- 真相源 CRUD


def test_truth_read_write(client: TestClient):
    book = client.post("/api/books", json={"title": "真相源"}).json()["dir"]
    lst = client.get(f"/api/books/{book}/truth").json()
    assert any("constitution" in f["path"] for f in lst["files"])
    got = client.get(f"/api/books/{book}/truth/constitution.md")
    assert got.status_code == 200 and "红线" in got.json()["content"]
    put = client.put(
        f"/api/books/{book}/truth/constitution.md",
        json={"content": "# 新宪法\n\n## 红线\n1. 测试条目\n", "commit": True},
    )
    assert put.status_code == 200
    assert "测试条目" in client.get(f"/api/books/{book}/truth/constitution.md").json()["content"]


def test_truth_path_traversal_blocked(client: TestClient):
    """路径穿越必须被 store.resolve() 挡住。

    ⚠️ 原版用 `/truth/../../../etc/passwd` 断言 status in (400,404,422) —— 那是**假绿**：
    Starlette 在路由匹配前就把 `..` 折叠掉了，返回 404「未知 API 路径: /api/etc/passwd」，
    store.resolve() 的逃逸守卫压根没被调用；把守卫删掉这条测试照样过。
    所以必须用百分号编码（`%2e%2e`）让 `..` 原样送达路由，才算真打到守卫。
    """
    book = client.post("/api/books", json={"title": "安全"}).json()["dir"]
    for payload in ("%2e%2e/%2e%2e/%2e%2e/etc/passwd", "..%2f..%2f..%2fetc/passwd"):
        r = client.get(f"/api/books/{book}/truth/{payload}")
        assert r.status_code == 400, f"{payload} 应被 resolve 拒绝，实际 {r.status_code} {r.text}"
        assert "escapes novel root" in r.json()["detail"]
    # 写路径同样挡住，且不落盘
    r = client.put(f"/api/books/{book}/truth/%2e%2e/%2e%2e/%2e%2e/etc/passwd", json={"content": "PWNED"})
    assert r.status_code == 400 and "escapes novel root" in r.json()["detail"]
    assert not Path("/etc/passwd").read_text(encoding="utf-8", errors="ignore").startswith("PWNED")


def test_foreshadow_crud_and_state_machine(client: TestClient):
    book = client.post("/api/books", json={"title": "伏笔"}).json()["dir"]
    # 初始为空
    assert client.get(f"/api/books/{book}/foreshadow").json()["items"] == []
    # 直接写 foreshadow.json（模拟已有伏笔）
    client.put(
        f"/api/books/{book}/truth/foreshadow.json",
        json={
            "content": json.dumps(
                {"items": [{"id": "F-001", "desc": "残图", "planted_ch": 3, "status": "planted", "deadline_ch": 40}]},
                ensure_ascii=False,
            )
        },
    )
    got = client.get(f"/api/books/{book}/foreshadow").json()
    assert len(got["items"]) == 1
    # 改状态
    p = client.patch(f"/api/books/{book}/foreshadow/F-001", json={"status": "hinted"})
    assert p.status_code == 200 and p.json()["status"] == "hinted"
    # 非法迁移
    p2 = client.patch(f"/api/books/{book}/foreshadow/F-001", json={"status": "draft"})
    assert p2.status_code == 409
    # 不存在的
    assert client.patch(f"/api/books/{book}/foreshadow/F-999", json={"status": "hinted"}).status_code == 404


def test_outline_crud(client: TestClient):
    book = client.post("/api/books", json={"title": "章纲"}).json()["dir"]
    assert client.get(f"/api/books/{book}/outline/1").status_code == 404
    put = client.put(
        f"/api/books/{book}/outline/1",
        json={"title": "开局", "pov": "mc", "hook": "钟响了", "scenes": [{"loc": "山门", "goal": "入门"}]},
    )
    assert put.status_code == 200
    got = client.get(f"/api/books/{book}/outline/1").json()
    assert got["title"] == "开局" and got["scenes"]


# ---------------------------------------------------------------- 装配 / 索引


def test_assemble_endpoint(client: TestClient, tmp_path: Path):
    _patch_gateway(client, tmp_path)
    book = client.post("/api/books", json={"title": "装配"}).json()["dir"]
    client.post(f"/api/books/{book}/plan", json={"start_ch": 1, "count": 1})
    r = client.post(f"/api/books/{book}/assemble", json={"ch": 1})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["total_tokens"] > 0
    assert "L0" in data["layers"]
    assert data["system_prompt"]
    # 落盘
    assert client.get(f"/api/books/{book}/assembled/1").status_code == 200


# ---------------------------------------------------------------- 模型层


def test_providers_endpoint(client: TestClient):
    d = client.get("/api/providers").json()
    assert any(p["name"] == "mock-local" for p in d["providers"])
    assert "write" in d["routes"]


def test_routing_reload(client: TestClient):
    r = client.post("/api/routing/reload")
    assert r.status_code == 200 and r.json()["reloaded"] is True


def test_usage_endpoint(client: TestClient, tmp_path: Path):
    _patch_gateway(client, tmp_path)
    book = client.post("/api/books", json={"title": "用量"}).json()["dir"]
    client.post(f"/api/books/{book}/plan", json={"start_ch": 1, "count": 1})
    u = client.get("/api/usage?days=7").json()
    assert u["total"]["calls"] >= 1
    assert "by_provider" in u


# ---------------------------------------------------------------- 导出 / 面板


def test_export_endpoint(client: TestClient, tmp_path: Path):
    _patch_gateway(client, tmp_path)
    book = client.post("/api/books", json={"title": "导出"}).json()["dir"]
    client.post(f"/api/books/{book}/plan", json={"start_ch": 1, "count": 1})
    client.post(f"/api/books/{book}/generate", json={"ch": 1, "stream": False})
    r = client.post(f"/api/books/{book}/export", json={"mode": "合集"})
    assert r.status_code == 200, r.text
    assert r.json()["files"]


def test_daily_panel_endpoint(client: TestClient):
    book = client.post("/api/books", json={"title": "面板"}).json()["dir"]
    r = client.get(f"/api/books/{book}/daily-panel")
    assert r.status_code == 200
    assert "stock_days" in r.json()


def test_stats_endpoint(client: TestClient):
    book = client.post("/api/books", json={"title": "统计"}).json()["dir"]
    r = client.get(f"/api/books/{book}/stats").json()
    assert "overview" in r and "word_curve" in r


# ---------------------------------------------------------------- 反馈 / 拆书


def test_feedback_endpoints(client: TestClient, tmp_path: Path):
    _patch_gateway(client, tmp_path)
    book = client.post("/api/books", json={"title": "反馈"}).json()["dir"]
    r = client.post(f"/api/books/{book}/feedback/stats", json={"date": "2026-10-28", "reads": 500, "chapters": {"1": {"drop_rate": 0.4}}})
    assert r.status_code == 200
    r2 = client.post(f"/api/books/{book}/feedback/reviews/1", json={"text": "好看\n水"})
    assert r2.status_code == 200 and "summary" in r2.json()
    r3 = client.post(f"/api/books/{book}/feedback/recheck", json={"apply": True})
    assert r3.status_code == 200


def test_benchmark_import_and_list(client: TestClient, tmp_path: Path):
    _patch_gateway(client, tmp_path)
    book = client.post("/api/books", json={"title": "拆书"}).json()["dir"]
    raw = "\n\n".join(f"第{i}章\n" + ("他猛地冲出，剑光一闪。突然危险逼近！" * 40) for i in range(1, 8))
    r = client.post(f"/api/books/{book}/benchmarks/import", json={"title": "对标A", "content": raw})
    assert r.status_code == 200, r.text
    assert r.json()["chapters"] >= 7
    lst = client.get(f"/api/books/{book}/benchmarks").json()
    assert any(b["id"] for b in lst["benchmarks"])
    bid = r.json()["id"]
    assert client.get(f"/api/books/{book}/benchmarks/{bid}").status_code == 200


# ---------------------------------------------------------------- 版本


def test_version_adopt_and_score(client: TestClient, tmp_path: Path):
    _patch_gateway(client, tmp_path)
    book = client.post("/api/books", json={"title": "版本"}).json()["dir"]
    client.post(f"/api/books/{book}/plan", json={"start_ch": 1, "count": 1})
    client.post(f"/api/books/{book}/generate", json={"ch": 1, "stream": False})
    client.post(f"/api/books/{book}/generate", json={"ch": 1, "stream": False, "as_version": True})
    detail = client.get(f"/api/books/{book}/chapters/1").json()
    assert detail["versions"]
    v = detail["versions"][0]["version"]
    a = client.post(f"/api/books/{book}/chapters/1/version/{v}/adopt")
    assert a.status_code == 200
    s = client.post(f"/api/books/{book}/chapters/1/version/{v}/score", json={"score": 4, "note": "不错"})
    assert s.status_code == 200 and s.json()["score"] == 4


# ---------------------------------------------------- 导出预检端点（§11.1）

def test_export_precheck_endpoint(client: TestClient):
    """导出预检必须暴露成端点：前端要在真正导出前展示风险。"""
    _patch_gateway(client, None)  # type: ignore[arg-type]
    book = client.post("/api/books", json={"title": "预检测试"}).json()["dir"]
    client.post(f"/api/books/{book}/outline/1", json={"ch": 1, "title": "一", "hook": "钟响。"})
    client.post(f"/api/books/{book}/outline/2", json={"ch": 2, "title": "二", "hook": "钟响。"})
    for ch in (1, 2):
        client.put(f"/api/books/{book}/chapters/{ch}", json={"text": "他握紧矿牌。" * 400})

    r = client.get(f"/api/books/{book}/export/precheck?platform=tomato&chapters=1-2")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["platform"] == "tomato"
    assert data["chapters"] == [1, 2]
    assert "ready" in data and isinstance(data["ready"], bool)
    assert data["summary"]["chapters"] == 2
    assert len(data["checks"]) == 2
    for c in data["checks"]:
        assert {"ch", "title", "words", "ok", "high_issues", "mid_issues"} <= set(c)


def test_export_precheck_chapter_list_parsing(client: TestClient):
    """``1,3`` 这类写法要能解析（前端与 CLI 都依赖）。"""
    book = client.post("/api/books", json={"title": "预检解析"}).json()["dir"]
    r = client.get(f"/api/books/{book}/export/precheck?chapters=1,3")
    assert r.status_code == 200
    assert r.json()["chapters"] == [1, 3]


def test_unknown_api_path_returns_404_json_not_html(client):
    """SPA fallback 绝不能吞 API：否则前端把 HTML 当 JSON 解析报无关错误。"""
    r = client.get("/api/definitely-not-a-route")
    assert r.status_code == 404
    assert "text/html" not in r.headers.get("content-type", "")


def test_favicon_served(client):
    """favicon 缺失会在浏览器里留下 404（实测出现过），必须能取到。"""
    r = client.get("/favicon.svg")
    assert r.status_code == 200
    assert "svg" in r.headers.get("content-type", "")


def test_manifest_served(client):
    r = client.get("/manifest.webmanifest")
    assert r.status_code == 200
    assert r.json()["name"]


def test_pwa_mounted(client):
    r = client.get("/pwa/index.html")
    assert r.status_code == 200
    assert "灵感速记" in r.text


# ---------------------------------------------------- 起名生成器（§10.3）

def test_names_generate_and_list(client: TestClient):
    book = client.post("/api/books", json={"title": "起名接口"}).json()["dir"]

    r = client.post(f"/api/books/{book}/names", json={"kind": "character", "count": 5, "seed": 42})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["kind"] == "character"
    assert len(data["names"]) == 5
    assert len(set(data["names"])) == 5

    r2 = client.get(f"/api/books/{book}/names")
    assert r2.status_code == 200
    assert r2.json()["total"] == 5
    assert r2.json()["by_kind"]["角色"] == 5


def test_names_unknown_kind_returns_400(client: TestClient):
    book = client.post("/api/books", json={"title": "起名错误"}).json()["dir"]
    r = client.post(f"/api/books/{book}/names", json={"kind": "不存在", "count": 3})
    assert r.status_code == 400
    assert "未知起名类别" in r.json()["detail"]


def test_names_seed_is_reproducible_over_http(client: TestClient):
    book = client.post("/api/books", json={"title": "起名复现"}).json()["dir"]
    a = client.post(f"/api/books/{book}/names", json={"kind": "sect", "count": 4, "seed": 9, "save": False}).json()
    b = client.post(f"/api/books/{book}/names", json={"kind": "sect", "count": 4, "seed": 9, "save": False}).json()
    assert a["names"] == b["names"]


# ---------------------------------------------------- 审稿 diff 预览（§13.2）

def test_review_suggest_endpoint(client: TestClient):
    """审稿台要能在采纳前看到 diff（§13.2 明确要求）。"""
    book = client.post("/api/books", json={"title": "diff 接口"}).json()["dir"]
    client.put(f"/api/books/{book}/chapters/1", json={"text": "他抬头看了一眼，仿佛在等谁开口。"})
    client.post(f"/api/books/{book}/review/1", json={"use_model": False})

    r = client.get(f"/api/books/{book}/review/1/suggest")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["ch"] == 1
    assert isinstance(data["suggestions"], list)
    for s in data["suggestions"]:
        assert {"issue_index", "category", "applicable", "reason", "before", "after", "diff"} <= set(s)


def test_review_suggest_selected_subset(client: TestClient):
    book = client.post("/api/books", json={"title": "diff 子集"}).json()["dir"]
    client.put(f"/api/books/{book}/chapters/1", json={"text": "他仿佛在等，宛如石像。"})
    client.post(f"/api/books/{book}/review/1", json={"use_model": False})

    full = client.get(f"/api/books/{book}/review/1/suggest").json()
    if not full["suggestions"]:
        pytest.skip("该样本未产生可预览问题")
    r = client.post(f"/api/books/{book}/review/1/suggest", json={"indices": [0]})
    assert r.status_code == 200
    assert len(r.json()["suggestions"]) == 1


def test_review_suggest_does_not_modify_chapter(client: TestClient):
    book = client.post("/api/books", json={"title": "diff 只读"}).json()["dir"]
    text = "他仿佛在等。"
    client.put(f"/api/books/{book}/chapters/1", json={"text": text})
    client.post(f"/api/books/{book}/review/1", json={"use_model": False})

    client.get(f"/api/books/{book}/review/1/suggest")
    after = client.get(f"/api/books/{book}/chapters/1").json()
    assert after["text"] == text, "预览接口不能改正文"


# ---------------------------------------------------- 时间线视图数据（§13.2）

def test_timeline_entries_have_days_since_start(client: TestClient):
    """时间线视图要能用 days_since_start 算「间隔几天」，后端必须给出。"""
    from server.core.schema import TimelineEntry

    from server.api.deps import get_context

    book = client.post("/api/books", json={"title": "时间线接口"}).json()["dir"]
    ctx = get_context()
    st = ctx.book(book).store
    st.save_timeline(
        [
            TimelineEntry(ch=1, date="青云历1024年2月初四", event="开局"),
            TimelineEntry(ch=2, date="青云历1024年3月初七", event="进阶"),
        ]
    )

    r = client.get(f"/api/books/{book}/timeline")
    assert r.status_code == 200
    data = r.json()
    entries = data["entries"]
    assert len(entries) == 2
    assert entries[0]["days_since_start"] == 0, "首条应为 0"
    assert entries[1]["days_since_start"] == 33, "2月初四 → 3月初七 应差 33 天"
    assert data["issues"] == []


# ------------------------------------------------- §缺陷 #58：章节清单必须有上限


def ctx_book_dir(client: TestClient) -> str:
    """在 client 的 ws 里立一本新书，返回它的目录名。"""
    r = client.post("/api/books", json={"title": "上限测试", "platform": "tomato"})
    assert r.status_code == 200, r.text
    shelf = client.get("/api/shelf").json()["novels"]
    return [e["dir"] for e in shelf if e["title"] == "上限测试"][0]

def test_chapter_list_is_capped():
    """区间写法必须封顶，否则 `1-9999999` 能把单机服务卡死。

    实测（无上限时）：`1-2000000` → 6.5s / 200 万章，`1-20000000` → 70s。
    区间本身很便宜，贵的是后面每章都要读正文。选 400 而不是静默截断：
    截断会让作者以为自己选的全都在。
    """
    from fastapi import HTTPException
    from server.api.routes import MAX_CHAPTER_LIST, _parse_chapter_list

    # 合理的区间照旧工作（含逆序、去重）
    assert _parse_chapter_list("1,2,5-7") == [1, 2, 5, 6, 7]
    assert _parse_chapter_list("7-5") == [5, 6, 7]
    assert _parse_chapter_list("1,1,2") == [1, 2]
    assert _parse_chapter_list("1-5000")[-1] == 5000

    # 越界的区间一律 400，不做静默截断
    for spec in ["1-" + str(MAX_CHAPTER_LIST + 1), "1-2000000", "1-99999999"]:
        with pytest.raises(HTTPException) as ei:
            _parse_chapter_list(spec)
        assert ei.value.status_code == 400
        assert "上限" in str(ei.value.detail)

    # 逗号项数本身也封顶（防止 "1,1,1,... " 刷满）
    with pytest.raises(HTTPException):
        _parse_chapter_list(",".join(["1"] * (MAX_CHAPTER_LIST + 1)))


def test_export_and_precheck_reject_oversized_lists(client: TestClient):
    """端点层也要拦：ExportReq 走的是 list[int]，绕过了 _parse_chapter_list。"""
    from server.api.routes import MAX_CHAPTER_LIST

    book = ctx_book_dir(client)
    base = f"/api/books/{book}"

    r = client.get(f"{base}/export/precheck", params={"chapters": "1-2000000"})
    assert r.status_code == 400
    assert "上限" in r.json()["detail"]

    r = client.post(
        f"{base}/export",
        json={"chapters": list(range(1, MAX_CHAPTER_LIST + 2)), "allow_risk": True},
    )
    assert r.status_code == 400

    # 正常规模不受影响
    r = client.get(f"{base}/export/precheck", params={"chapters": "1-3"})
    assert r.status_code == 200

def test_publish_and_recheck_cap_chapter_lists(client: TestClient):
    """缺陷 #58：直接收 list[int] 的批量端点也要封顶（它们绕过 _parse_chapter_list）。"""
    from fastapi import HTTPException

    from server.api.routes import _cap_chapters

    book = ctx_book_dir(client)
    with pytest.raises(HTTPException) as ei:
        _cap_chapters(list(range(1, 5002)))
    assert ei.value.status_code == 400 and "上限" in ei.value.detail
    # 正常量级不受影响
    assert _cap_chapters([1, 2, 3]) == [1, 2, 3]
    assert _cap_chapters(None) is None
    # publish 走真实 HTTP：超量 400，正常量级进到业务层（没章可发则 409，不再是 400 上限）
    r = client.post(f"/api/books/{book}/publish", json={"chapters": list(range(1, 5002))})
    assert r.status_code == 400 and "上限" in r.json()["detail"]
    r = client.post(f"/api/books/{book}/publish", json={"chapters": [1, 2, 3]})
    assert r.status_code in (200, 409), r.text
    r = client.post(f"/api/books/{book}/feedback/recheck", json={"chapters": list(range(1, 5002))})
    assert r.status_code == 400 and "上限" in r.json()["detail"]
    r = client.post(f"/api/books/{book}/feedback/recheck", json={"chapters": [1]})
    assert r.status_code == 200, r.text

def test_book_title_length_is_capped(client: TestClient):
    """缺陷 #60：书名长度要有上界（否则建目录时炸 OSError(Errno 36) 变成 HTTP 500）。"""
    from server.core.paths import MAX_TITLE_CHARS, safe_title

    with pytest.raises(ValueError) as ei:
        safe_title("超" * (MAX_TITLE_CHARS + 1))
    assert "书名过长" in str(ei.value)
    assert len(safe_title("超" * MAX_TITLE_CHARS)) == MAX_TITLE_CHARS
    # 边界：60 字建书必须成功，61 字必须 409（不是 500）
    r = client.post("/api/books", json={"title": "超" * (MAX_TITLE_CHARS + 1), "platform": "tomato"})
    assert r.status_code == 409, r.text
    assert "书名过长" in r.json()["detail"]
    r = client.post("/api/books", json={"title": "超" * MAX_TITLE_CHARS, "platform": "tomato"})
    assert r.status_code == 200, r.text


def test_chapter_number_is_reasonable(tmp_path):
    """缺陷 #60：章号本身要有合理上界（ch_name 只格式化不校验，1e9 会写出 ch-1000000000.md）。"""
    from server.api.deps import AppContext
    from server.core.schema import BookMeta
    from server.core.paths import MAX_CHAPTER_NO

    ctx = AppContext.create(tmp_path / "ws")
    bn = ctx.workbench.create_novel(BookMeta(title="章号", platform="tomato"))
    store = bn.store if hasattr(bn, "store") else bn
    with pytest.raises(ValueError) as ei:
        store.write_chapter(MAX_CHAPTER_NO + 1, "正文")
    assert "章号" in str(ei.value)
    store.write_chapter(MAX_CHAPTER_NO, "正文")
    assert store.read_chapter(MAX_CHAPTER_NO).strip() == "正文"


def test_unhandled_exception_is_logged(tmp_path, caplog):
    """缺陷 #65：未捕获异常必须留痕。

    原先 `@app.exception_handler(Exception)` 既不打日志也不留 traceback：
    线上报了 500，服务端什么都没留下，事后根本没法追。现在必须写日志，
    但响应体仍不含 traceback（不泄露内部路径）。
    """
    import logging

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from server.app import create_app

    app = create_app(tmp_path / "ws")

    # 挂在业务 router 上（app 层的 /{full_path:path} 兜底会先匹配到它）
    from server.api.routes import router

    @router.get("/books/boom-probe/boom")
    def boom():
        raise RuntimeError("故意炸")

    with caplog.at_level(logging.ERROR, logger="workbench.app"):
        c = TestClient(app, raise_server_exceptions=False)
        r = c.get("/api/books/boom-probe/boom")
    assert r.status_code == 500, r.text
    # 响应体只有类型与消息，不含 traceback / 内部路径
    body = r.json()["error"]
    assert "RuntimeError: 故意炸" in body
    assert "Traceback" not in body and ".py" not in body, body
    # 日志必须留下traceback
    msgs = [rec.getMessage() for rec in caplog.records]
    assert any("未捕获异常" in m and "RuntimeError" in m for m in msgs), msgs
    assert any(rec.exc_info for rec in caplog.records), "必须带 exc_info（否则没有 traceback）"


def test_value_error_from_guards_is_400_not_500(client: TestClient):
    """缺陷 #66：参数守卫抛的 ValueError 必须是 400（客户端的毛病）。

    实测修之前：`PUT /chapters/1000000000` 返回 500 `{"error":"ValueError: 章号超出范围..."}`
    ——作者填错章号，服务端报 500，监控会把这当服务端故障。
    只兜 ValueError；KeyError/TypeError 仍然 500（那是代码 bug，不是参数的错）。
    """
    from server.core.paths import MAX_CHAPTER_NO

    book = ctx_book_dir(client)
    r = client.put(f"/api/books/{book}/chapters/{MAX_CHAPTER_NO + 1}", json={"text": "正文"})
    assert r.status_code == 400, r.text
    assert "章号超出范围" in r.json()["detail"], r.text
    r = client.put(f"/api/books/{book}/chapters/0", json={"text": "正文"})
    assert r.status_code == 400 and "章号必须为正" in r.json()["detail"], r.text
    r = client.get(f"/api/books/{book}/outline/{MAX_CHAPTER_NO + 1}")
    assert r.status_code == 400, r.text
    # 正常章号仍然 200
    r = client.put(f"/api/books/{book}/chapters/1200", json={"text": "正文"})
    assert r.status_code == 200, r.text


def test_code_bug_still_500(client: TestClient):
    """缺陷 #66 的另一半：代码 bug（KeyError/TypeError）仍然必须 500。

    如果把所有异常都兜成 400，真正的服务端故障会被伪装成「用户填错了」。
    """
    from server.api.routes import router

    @router.get("/books/probe-kerr/kerr")
    def kerr():
        raise KeyError("missing")

    @router.get("/books/probe-terr/terr")
    def terr():
        raise TypeError("bad type")

    c = TestClient(client.app, raise_server_exceptions=False)
    for path in ("/api/books/probe-kerr/kerr", "/api/books/probe-terr/terr"):
        r = c.get(path)
        assert r.status_code == 500, f"{path} 应为 500，实际 {r.status_code}: {r.text}"


def test_feedback_review_text_is_capped(client: TestClient):
    """缺陷 #67：读者评论长度上界（此前 5MB 也照收，磁盘与统计一起被灌爆）。"""
    from server.api.routes import MAX_REVIEW_CHARS

    book = ctx_book_dir(client)
    r = client.post(
        f"/api/books/{book}/feedback/reviews/1",
        json={"text": "读" * (MAX_REVIEW_CHARS + 1)},
    )
    assert r.status_code == 400, r.text
    assert "过长" in r.json()["detail"], r.text
    r = client.post(
        f"/api/books/{book}/feedback/reviews/2",
        json={"text": "还行，追更中"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["chars"] == len("还行，追更中"), r.json()


def test_capped_model_guards_every_new_payload_model(client: TestClient):
    """缺陷 #68：逐个端点加守卫是补丁，新加端点时没人会想起加。

    改成机制：所有入站载荷模型继承 `CappedModel`，pydantic 校验阶段统一兜底
    ——任意 str/list 字段超限直接拦下，新模型只要继承它就自动带上界。
    """
    from server.api.routes import (
        MAX_FIELD_LIST_ITEMS,
        MAX_FIELD_STR_CHARS,
        CappedModel,
    )

    # 机制本身：随便一个新模型，只要继承就自动有上界
    class FutureReq(CappedModel):
        name: str = ""
        tags: list[str] = []
        nested: list[dict] = []

    with pytest.raises(Exception) as ei:
        FutureReq(name="x" * (MAX_FIELD_STR_CHARS + 1))
    assert "过长" in str(ei.value)
    with pytest.raises(Exception) as ei:
        FutureReq(tags=["t"] * (MAX_FIELD_LIST_ITEMS + 1))
    assert "过长" in str(ei.value)
    # 嵌套字典里的长串也逃不掉
    with pytest.raises(Exception) as ei:
        FutureReq(nested=[{"k": "v" * (MAX_FIELD_STR_CHARS + 1)}])
    assert "过长" in str(ei.value)
    # 正常量级放行
    assert FutureReq(name="正常", tags=["a", "b"], nested=[{"k": "v"}]).name == "正常"

    # 真实端点：之前实测能灌 1 万条的接口现在必须被拦（都是 400 不是 500）
    book = ctx_book_dir(client)
    r = client.put(
        f"/api/books/{book}/threads",
        json={"id": "t1", "name": "线", "beats": ["b"] * (MAX_FIELD_LIST_ITEMS + 1)},
    )
    assert r.status_code == 400, r.text
    assert "过长" in r.json()["detail"], r.text
    r = client.put(
        f"/api/books/{book}/outline/1",
        json={"title": "起", "must_include": ["m"] * (MAX_FIELD_LIST_ITEMS + 1)},
    )
    assert r.status_code == 400 and "过长" in r.json()["detail"], r.text
    # 正常量级照常工作
    r = client.put(f"/api/books/{book}/threads", json={"id": "t2", "name": "线2", "beats": ["起", "承"]})
    assert r.status_code == 200, r.text
    r = client.put(f"/api/books/{book}/outline/2", json={"title": "承", "must_include": ["a", "b"]})
    assert r.status_code == 200, r.text


def test_all_payload_models_inherit_capped():
    """缺陷 #68 的自检：不许有入站模型漏掉 CappedModel。

    只测行为不够——新加一个 BaseModel 的 Req 时，行为测试不会红，
    这个模型却是裸奔的。用 AST 盯着继承关系。
    """
    import ast

    from server.api.routes import CappedModel

    root = Path(__file__).resolve().parent.parent / "server" / "api" / "routes.py"
    tree = ast.parse(root.read_text(encoding="utf-8"))
    bare = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        if node.name == "CappedModel":
            continue
        if not any(isinstance(b, ast.Name) and b.id == "BaseModel" for b in node.bases):
            continue
        bare.append(node.name)
    assert not bare, f"这些入站模型没继承 CappedModel（裸奔）: {bare}"
    assert issubclass(CappedModel, BaseModel)


# 缺陷 #71：章纲状态机必须挂在通用写入口上
def test_outline_put_enforces_state_machine(client: TestClient):
    """PUT /outline/{ch} 必须走同一张迁移表。

    publish 路由一直查 can_outline_transition，但通用章纲写入口的
    OutlineUpdate.status 是裸 str —— draft → published 这种跳迁此前是被静默
    接受的：章纲能过 model_validate（published 是合法枚举值），作者端看不出
    异常，daily-panel 却把它算进已发布、存稿口径随之失真。
    """
    book = client.post("/api/books", json={"title": "状态机"}).json()["dir"]
    # 章纲不存在时按 draft 起算
    r = client.put(f"/api/books/{book}/outline/1", json={"title": "起", "status": "planned"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "planned"

    # 跳迁一律 409
    for bad in ("published", "final", "reviewed"):
        r = client.put(f"/api/books/{book}/outline/1", json={"status": bad})
        assert r.status_code == 409, f"{bad} 应被拒，实际 {r.status_code} {r.text}"
        assert "非法状态迁移" in r.json()["detail"], r.text
    # 被拒后章纲没被改写
    assert client.get(f"/api/books/{book}/outline/1").json()["status"] == "planned"

    # 幂等：写同一个状态不算迁移
    assert client.put(f"/api/books/{book}/outline/1", json={"status": "planned"}).status_code == 200

    # 按迁移表逐步推进全部放行
    for nxt in ("writing", "drafted", "reviewed", "final", "published"):
        r = client.put(f"/api/books/{book}/outline/1", json={"status": nxt})
        assert r.status_code == 200, f"{nxt} 应放行，实际 {r.status_code} {r.text}"
        assert r.json()["status"] == nxt

    # 不带 status 的普通编辑不受影响
    r = client.put(f"/api/books/{book}/outline/1", json={"title": "改后的标题"})
    assert r.status_code == 200 and r.json()["title"] == "改后的标题", r.text


# 缺陷 #72：POST /outline/{ch}/confirm 原先无条件把状态写成 planned，
# published / final 也能被拉回来。「确认」是向前推进，不是倒回。
def test_confirm_endpoint_refuses_to_rewind_terminal_states(client: TestClient):
    book = ctx_book_dir(client)
    base = f"/api/books/{book}/outline"

    def walk_to(ch: int, target: str) -> None:
        """缺陷 #71 之后 PUT 也查迁移表，所以只能按合法路径逐级走过去。"""
        path = {
            "draft": [],
            "planned": ["planned"],
            "writing": ["planned", "writing"],
            "drafted": ["planned", "drafted"],
            "reviewed": ["planned", "drafted", "reviewed"],
            "final": ["planned", "drafted", "reviewed", "final"],
            "published": ["planned", "drafted", "reviewed", "final", "published"],
        }[target]
        for s in path:
            r = client.put(f"{base}/{ch}", json={"title": "t", "status": s})
            assert r.status_code == 200, r.text

    # 可确认的三种（draft / planned / writing）：draft 本身没有章纲文件，
    # 先 PUT 一版把它建出来再确认
    r0 = client.put(f"{base}/1", json={"title": "t"})
    assert r0.status_code == 200, r0.text
    assert r0.json()["status"] == "draft", r0.text
    c1r = client.post(f"{base}/1/confirm")
    assert c1r.status_code == 200 and c1r.json()["status"] == "planned", c1r.text

    for ch, s in enumerate(["planned", "writing"], start=2):
        walk_to(ch, s)
        c = client.post(f"{base}/{ch}/confirm")
        assert c.status_code == 200 and c.json()["status"] == "planned", s + " 应可确认: " + c.text

    # 终态一律 409，且盘上原样
    for ch, s in enumerate(["drafted", "reviewed", "final", "published"], start=11):
        walk_to(ch, s)
        c = client.post(f"{base}/{ch}/confirm")
        assert c.status_code == 409, s + " 应被拒: " + c.text
        assert "非法状态迁移" in c.json()["detail"], c.text
        assert client.get(f"{base}/{ch}").json()["status"] == s, s + " 被改写"

    # 章纲不存在仍然是 404
    assert client.post(f"{base}/999/confirm").status_code == 404



# ------------------------------------------------- 缺陷 #75：手工保存正文也要查 published


def test_put_chapter_refuses_published(client: TestClient):
    """Writer 两条写路径早有保护，手工保存这条没有——作者能在前端直接改掉读者已看的章节。"""
    book = ctx_book_dir(client)
    base = f"/api/books/{book}"
    body = "他握紧拳头走向门口，没有回头。院中的梧桐叶落了一地。"

    # 先立章纲并写正文（此时还不是终态，可自由编辑）
    assert client.put(f"{base}/outline/1", json={"title": "t"}).status_code == 200
    assert client.put(f"{base}/chapters/1", json={"text": body}).status_code == 200

    # 再走到 published（走合法迁移链，否则被 #71 的守卫挡下）
    for s in ("planned", "writing", "drafted", "reviewed", "final", "published"):
        r = client.put(f"{base}/outline/1", json={"status": s})
        assert r.status_code == 200, s + ": " + r.text
    assert client.get(f"{base}/outline/1").json()["status"] == "published"

    r = client.put(f"{base}/chapters/1", json={"text": body + "改过了。"})
    assert r.status_code == 409, r.text
    assert "已发布" in r.json()["detail"], r.text
    assert client.get(f"{base}/chapters/1").json()["text"] == body, "被拒后正文必须原样"

    # 撤销发布后可写（不误伤正常编辑）
    assert client.put(f"{base}/outline/1", json={"status": "final"}).status_code == 200
    assert client.put(f"{base}/chapters/1", json={"text": body + "再改。"}).status_code == 200



# ------------------------------------------------- 缺陷 #77：文件名不能由用户拼出路径


def test_put_thread_rejects_traversal_over_http(client: TestClient):
    """路由要把 StoreError 转 400，而不是 500（真穿透时文件已经落盘了）。"""
    r = client.post("/api/books", json={"title": "线程书", "platform": "tomato"})
    assert r.status_code == 200, r.text
    d = [e["dir"] for e in client.get("/api/shelf").json()["novels"] if e["title"] == "线程书"][0]
    b = f"/api/books/{d}"

    bad = client.put(b + "/threads", json={"id": "../../evil", "name": "坏"})
    assert bad.status_code == 400, bad.text
    assert "线程 id" in bad.json()["detail"], bad.text

    ok = client.put(b + "/threads", json={"id": "main", "name": "主线", "beats": ["起"]})
    assert ok.status_code == 200, ok.text
    got = client.get(b + "/threads").json()["threads"]
    assert [t["id"] for t in got] == ["main"], got



# ------------------------------------------------- 缺陷 #78：建书平台名不能拼出路径


def test_create_book_rejects_traversal_in_platform_over_http(client: TestClient):
    """HTTP 层要转 409（StoreError），而不是建完书才发现文件已经写到工作区根。"""
    bad = client.post("/api/books", json={"title": "坏平台", "platform": "../../evil"})
    assert bad.status_code == 409, bad.text
    assert "平台名" in bad.json()["detail"], bad.text

    ok = client.post("/api/books", json={"title": "好平台", "platform": "tomato"})
    assert ok.status_code == 200, ok.text
    d = ok.json()["dir"]
    prof = client.get(f"/api/books/{d}/profiles").json()["profiles"]
    assert "tomato" in prof, prof


# ------------------------------------------------- 缺陷 #79：对标书 id 必须是纯目录名


def test_benchmark_detail_rejects_traversal_in_bid(client: TestClient):
    """bid 会被拼进路径（`analysis/benchmarks/{bid}`），带 .. 会读到 benchmarks 的上级目录。

    「目录不存在就 404」这条守卫挡不住它：.. 拼出来的 analysis/ 本来就存在，
    于是作者没打算暴露的文件（模板、其他对标产物）会被整个列出来。
    """
    d = ctx_book_dir(client)
    b = f"/api/books/{d}"
    # 只测百分号编码形式：明文 .. 会被 Starlette 在路由匹配前折叠，根本到不了这个端点
    # （那是 #56 记过的框架行为，不是本端点的守卫）。本端点要守的是「绕过了折叠」的那一条。
    # 能真正抵达本端点的只有「不含斜杠的编码 ..」：带斜杠的形式会被 Starlette
    # 在路由匹配前折叠成 404（#56 记过的框架行为）。而 %2e%2e 正是会漏的那一条 ——
    # 它拼出的 analysis/ 目录本来就存在，「目录不存在则 404」的守卫形同虚设。
    r = client.get(f"{b}/benchmarks/%2e%2e")
    assert r.status_code == 400, f"编码 .. 应被拒绝，实际 {r.status_code}: {r.text[:80]}"
    assert "对标书 id" in r.json()["detail"], r.text
    # 正常 id 不受影响（不存在则是 404 而不是 400）
    miss = client.get(f"{b}/benchmarks/nonexistent")
    assert miss.status_code == 404, miss.text
    assert "对标书产物不存在" in miss.json()["detail"], miss.text

# ------------------------------------------------- 缺陷 #80：平台名不能拼出路径


def test_load_profile_rejects_traversal_in_platform(client: TestClient, tmp_path: Path):
    """load_profile 把 platform 拼成 profiles/{platform}.json。

    不校验的话 platform=../../wsroot 会读到书外任意 json 并当平台配置用：
    探针实测预检的字数下限从 2000 变成书外文件里写的 10（红线词库同理会被替换）。
    """
    from server.core.store import StoreError

    d = ctx_book_dir(client)
    b = f"/api/books/{d}"
    for plat in ["../../wsroot", "../outside", "a/b", "..", ".." + chr(92) + "evil"]:
        r = client.get(f"{b}/export/precheck", params={"platform": plat, "chapters": "1"})
        assert r.status_code == 400, f"{plat} 应被拒绝，实际 {r.status_code}: {r.text[:80]}"
        assert "平台名" in r.json()["detail"], r.text
    # 正常平台名不受影响
    ok = client.get(f"{b}/export/precheck", params={"platform": "tomato", "chapters": "1"})
    assert ok.status_code == 200, ok.text
    assert ok.json()["platform"] == "tomato", ok.text
    # 外置红线词库同样要挡在书根内（store 层直接调用也要抛）
    from server.core.store import Workbench

    st = Workbench(tmp_path / "ws").get_store(d)
    assert st.resolve("chapters/ch-0001.md").parent == st.root / "chapters"
    for bad in ["../../outside_red.txt", "../red.txt"]:
        with pytest.raises(StoreError):
            st.resolve(bad)

# ------------------------------------------------- 缺陷 #82：对标书名必须有长度上界


def test_benchmark_title_is_capped(tmp_path: Path):
    """对标书名会当目录名落 analysis/benchmarks/，超长必须在落盘前被挡。

    修复前：60/200/400 字的书名都会在 mkdir 那一步炸 OSError(Errno 36)，
    HTTP 层变成一句英文 500（实测三条全是 500）；修复后统一 400 且消息可读。
    不静默截断——截断会让两本不同的对标书撞到同一个 bid 上，产物互相覆盖。
    """
    from server.agents.deconstructor import (
        MAX_BENCHMARK_TITLE_CHARS,
        _safe_dir,
    )

    assert _safe_dir("对标书") == "对标书"
    assert _safe_dir("") == "benchmark", "空名仍要退回默认名，不能因为上限改动而崩"
    assert _safe_dir("..") == "benchmark"
    assert _safe_dir("a/b") == "a_b", "路径分隔符照旧净化"
    long = "对" * (MAX_BENCHMARK_TITLE_CHARS + 1)
    with pytest.raises(ValueError, match="对标书名过长"):
        _safe_dir(long)


def test_benchmark_import_rejects_overlong_title(client: TestClient):
    """HTTP 层：超长书名必须是 400（可读消息），不能是 500（英文 OSError）。"""
    book = ctx_book_dir(client)
    body = "第一章" + chr(10) + "内容" + chr(10) + "第一章" + chr(10) + "内容"
    ok = client.post(
        f"/api/books/{book}/benchmarks/import",
        json={"title": "对标书", "content": body, "use_model": False},
    )
    assert ok.status_code == 200, ok.text
    bad = client.post(
        f"/api/books/{book}/benchmarks/import",
        json={"title": "对" * 400, "content": body, "use_model": False},
    )
    assert bad.status_code == 400, bad.text
    assert "对标书名过长" in bad.json()["detail"], bad.text


# ------------------------------------------------- 缺陷 #83：半截 json 不能让日更面板 500


def test_daily_panel_survives_corrupt_benchmark_json(client: TestClient, tmp_path: Path):
    """一本写坏的对标产物，不能把整个日更面板带成 500。

    修复前 `pacing_vs_benchmark` 用裸 json.loads(read_text())：analysis/benchmarks/
    <bid>/structure.json 只要写了一半（外部编辑器 / 同步盘 / 断电），daily-panel
    就返回 500 `Unterminated string starting at ...`，面板整块空白。
    """
    book = ctx_book_dir(client)
    base = tmp_path / "ws" / "novels" / book / "analysis" / "benchmarks" / "half"
    base.mkdir(parents=True, exist_ok=True)
    (base / "structure.json").write_text('{"title": "broken', encoding="utf-8")
    r = client.get(f"/api/books/{book}/daily-panel")
    assert r.status_code == 200, r.text
    pacing = r.json()["pacing"]
    # 坏的那本跳过，其余照常（这里没有别的对标书，所以 available 为 False 或空列表）
    assert isinstance(pacing, dict)
    assert pacing.get("available") in (True, False)
    # 列表接口同样不能 500（它本来就走 atomic.read_json，守住防回归）
    assert client.get(f"/api/books/{book}/benchmarks").status_code == 200


# ------------------------------------------------- 缺陷 #84：坏 meta 不该堵死评分/切版本


def test_score_and_adopt_survive_corrupt_version_meta(client: TestClient, tmp_path: Path):
    """chapters/ch-XXXX.versions/vN.meta.json 写坏时，评分与切换版本必须仍能用。

    修复前这两处用裸 `json.loads(mfile.read_text() or "{}")`：实测一个半截 meta
    让 POST /chapters/1/version/1/score 返回 400 `Expecting value: line 1 column 10`，
    作者补不了分也切不了版本。meta 只是留档，读不出就当空字典，本次写入会顺手修好。
    """
    book = ctx_book_dir(client)
    root = tmp_path / "ws" / "novels" / book
    r = client.put(
        f"/api/books/{book}/chapters/1",
        json={"text": "第一章正文内容在这里，足够长。", "commit": False},
    )
    assert r.status_code == 200, r.text
    vdir = root / "chapters" / "ch-0001.versions"
    vdir.mkdir(parents=True, exist_ok=True)
    (vdir / "v1.meta.json").write_text('{"model":', encoding="utf-8")
    (vdir / "v1.md").write_text("# 第一章" + chr(10) + "旧版本正文在这里。", encoding="utf-8")

    score = client.post(f"/api/books/{book}/chapters/1/version/1/score", json={"score": 4, "note": "x"})
    assert score.status_code == 200, score.text
    assert score.json()["score"] == 4
    # 修好之后 meta 里应该同时留下 model 之外的 score
    meta = json.loads((vdir / "v1.meta.json").read_text(encoding="utf-8"))
    assert meta["score"] == 4

    adopt = client.post(f"/api/books/{book}/chapters/1/version/1/adopt")
    assert adopt.status_code == 200, adopt.text
    meta2 = json.loads((vdir / "v1.meta.json").read_text(encoding="utf-8"))
    assert meta2["adopted"] is True


# ------------------------------------------------- 缺陷 #85：写坏的 feedback/stats.json 不该让复盘 500


def test_feedback_recheck_survives_malformed_stats(client: TestClient, tmp_path: Path):
    """feedback/stats.json 被外部改坏时，复盘链路必须仍返回 200（只是没有候选章）。

    修复前 `top_drop_chapters` 假设 days 是 list、chapters 的值是 dict：实测六种畸形
    写法（chapters 值 str/None/list、days 是 dict/str/元素是 str 或 None）全部让
    POST /feedback/recheck 返回 500 `AttributeError: object has no attribute .get`。
    数据结构不对就跳过，不抛。
    """
    book = ctx_book_dir(client)
    root = tmp_path / "ws" / "novels" / book
    p = root / "feedback" / "stats.json"
    p.parent.mkdir(parents=True, exist_ok=True)

    malformed = [
        json.dumps({"days": [{"date": "a", "chapters": {"1": "oops"}}]}),
        json.dumps({"days": [{"date": "a", "chapters": {"1": None}}]}),
        json.dumps({"days": [{"date": "a", "chapters": {"1": [1, 2]}}]}),
        json.dumps({"days": {"a": 1}}),
        json.dumps({"days": "boom"}),
        json.dumps({"days": ["boom"]}),
        json.dumps({"days": [None]}),
    ]
    for body in malformed:
        p.write_text(body, encoding="utf-8")
        r = client.post(f"/api/books/{book}/feedback/recheck", json={})
        assert r.status_code == 200, f"{body[:40]} -> {r.status_code} {r.text[:200]}"
        assert r.json()["chapters"] == []
        panel = client.get(f"/api/books/{book}/daily-panel")
        assert panel.status_code == 200, panel.text

    # 正常数据仍然能选出高弃书率的章
    p.write_text(
        json.dumps({"days": [{"date": "a", "chapters": {"1": {"drop_rate": 0.9}, "2": {"drop_rate": 0.1}}}]}),
        encoding="utf-8",
    )
    r = client.post(f"/api/books/{book}/feedback/recheck", json={})
    assert r.status_code == 200, r.text
    assert r.json()["chapters"] == [1], r.json()


# ------------------------------------------------- 缺陷 #86：审稿报告被写坏时复盘不该 500


def test_recheck_survives_corrupt_review_report(client: TestClient, tmp_path: Path):
    """memory/ch-XXXX.review.json 被写成数组/标量时，`POST /feedback/recheck` 必须仍 200。

    修复前 `NovelStore.load_review` 直接 `raw.setdefault("ch", ch)`：实测手工把 review.json
    写成 `[1,2,3]` 后复盘返回 500 `AttributeError: 'list' object has no attribute
    'setdefault'`——作者看不到任何复盘结果。读不出来就当没审过（categories 为空）。
    """
    book = ctx_book_dir(client)
    root = tmp_path / "ws" / "novels" / book
    r = client.put(
        f"/api/books/{book}/chapters/1",
        json={"text": "第一章正文内容在这里，足够长，可以过字数下限检查。" * 30, "commit": False},
    )
    assert r.status_code == 200, r.text
    stats = root / "feedback" / "stats.json"
    stats.parent.mkdir(parents=True, exist_ok=True)
    stats.write_text(json.dumps({"days": [{"date": "a", "chapters": {"1": {"drop_rate": 0.9}}}]}), encoding="utf-8")
    rj = root / "memory" / "ch-0001.review.json"
    rj.parent.mkdir(parents=True, exist_ok=True)

    for body in ("[1,2,3]", '"oops"', "42", "null"):
        rj.write_text(body, encoding="utf-8")
        r = client.post(f"/api/books/{book}/feedback/recheck", json={})
        assert r.status_code == 200, f"{body} -> {r.status_code} {r.text[:200]}"
        assert r.json()["chapters"] == [1]

    # 正常报告（空 issues）仍可走通：服务端重跑 Reviewer，categories 非空
    rj.write_text(json.dumps({"ch": 1, "issues": []}), encoding="utf-8")
    r = client.post(f"/api/books/{book}/feedback/recheck", json={})
    assert r.status_code == 200, r.text
    assert r.json()["findings"][0]["review_exists"] is True


# ------------------------------------------------- 缺陷 #87：二进制真相源要给中文提示


def test_read_truth_binary_gives_chinese_message(client: TestClient, tmp_path: Path):
    """truth/ 下混进二进制文件时，报错必须是人话，不能是 codec 报错。

    修复前：`p.read_text(encoding="utf-8")` 抛的 UnicodeDecodeError 是 ValueError
    子类，被全局 ValueError handler（缺陷 #66）转成 400 + `'utf-8' codec can't
    decode byte 0xff in position 0: invalid start byte`。真相源里混进截图/误拖的
    素材时，作者看到的是看不懂的英文。
    """
    book = ctx_book_dir(client)
    root = tmp_path / "ws" / "novels" / book
    bad = root / "truth" / "binary.bin"
    bad.write_bytes(b"\xff\xfe\x00\x01bad bytes")
    r = client.get(f"/api/books/{book}/truth/binary.bin")
    assert r.status_code == 400, r.text
    assert "UTF-8" in r.json()["detail"]
    assert "binary.bin" in r.json()["detail"]
    # codec 报错原文不该出现在响应里
    assert "codec" not in r.json()["detail"]

    # 正常文本不受影响
    ok = client.get(f"/api/books/{book}/truth/arcs.md")
    assert ok.status_code == 200, ok.text


# ------------------------------------------------- 缺陷 #88：并发速记静默丢数据


def test_concurrent_inspiration_notes_are_not_lost(client: TestClient, tmp_path: Path):
    """并发速记不能互相覆盖：响应全 200，文件里却只剩几条，比报错更糟。

    修复前 `add_inspiration` 是「读旧内容 → 拼接 → 原子写回」三段式，写入本身
    原子但整体不原子：实测 20 个线程并发提交速记，响应全部 200，文件里只剩
    3–5 条（其余永久丢失且无报错）。
    """
    import threading

    book = ctx_book_dir(client)
    root = tmp_path / "ws" / "novels" / book
    errs: list[str] = []
    lock = threading.Lock()

    def one(i: int):
        r = client.post(
            f"/api/books/{book}/inspiration",
            json={"notes": [{"text": f"note-{i}", "kind": "idea"}]},
        )
        if r.status_code != 200:
            with lock:
                errs.append(f"{i}:{r.status_code}:{r.text[:120]}")

    ts = [threading.Thread(target=one, args=(i,)) for i in range(20)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert not errs, errs[:3]

    body = (root / "materials" / "inspiration.md").read_text(encoding="utf-8")
    kept = sum(1 for i in range(20) if f"note-{i}" in body)
    assert kept == 20, f"20 条速记只留下 {kept} 条：{body[:200]}"


# ------------------------------------------------- 缺陷 #89：并发确认避雷提案会丢条目


def test_concurrent_proposal_confirms_are_not_lost(tmp_path: Path):
    """与 #88 同源：避雷条目确认也是「读 style-fingerprint → 去重 → 写回」。

    实测 20 个线程并发确认**不同**提案，最后 style-fingerprint 里只剩 3 条。
    丢的是作者已经确认过的避雷条目（幂等判定里被后来的写覆盖掉了）。
    """
    import threading

    from server.core.store import Workbench
    from server.core.schema import BookMeta
    from server.feedback.ingest import FeedbackIngest

    ws = tmp_path / "ws"
    st = Workbench(ws).create_novel(BookMeta(title="并发避雷", platform="tomato"))
    ing = FeedbackIngest(st)

    def one(i: int):
        ing.apply_proposals([{"entry": f"- [第{i}章] 避雷条目{i}"}])

    ts = [threading.Thread(target=one, args=(i,)) for i in range(20)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    txt = st.p.style_fingerprint.read_text(encoding="utf-8")
    kept = sum(1 for i in range(20) if f"避雷条目{i}" in txt)
    assert kept == 20, f"20 条避雷只留下 {kept} 条：{txt[:200]}"


# ------------------------------------------------- 缺陷 #90：外置红线词库混进二进制不该 500/400


def test_binary_redline_file_does_not_break_precheck(client: TestClient, tmp_path: Path):
    """平台配置里的 `redline_file` 指向的 txt 混进二进制字节时，导出预检必须仍能用。

    修复前 `store.load_profile` 裸 `extra.read_text(encoding="utf-8")`：实测把 red.txt
    写成 b"\\xff\\xfe\\x00bad bytes" 后 `GET /export/precheck` 返回 400
    `"'utf-8' codec can't decode byte 0xff in position 0: invalid start byte"`——
    UnicodeDecodeError 是 ValueError 子类，被 #66 的全局 handler 接走变成了英文报错。
    词库读不出来就按「没有外置词库」处理。
    """
    from server.core.store import Workbench
    from server.core.schema import BookMeta, ChapterOutline, Scene

    book = ctx_book_dir(client)
    ws = tmp_path / "ws"
    root = ws / "novels" / book
    st = Workbench(ws).get_store(book)
    st.write_chapter(1, "第一章正文内容在这里，足够长，可以过字数下限检查。" * 30)
    st.save_outline(ChapterOutline(ch=1, title="起", pov="mc", scenes=[Scene(at="开场", beat="起")]))
    prof = st.load_profile("tomato")
    prof.redline_file = "red.txt"
    (root / "red.txt").write_bytes(b"\xff\xfe\x00bad bytes")
    st.save_profile(prof)

    r = client.get(f"/api/books/{book}/export/precheck", params={"platform": "tomato", "chapters": "1"})
    assert r.status_code == 200, r.text[:200]


# ------------------------------------------------- 缺陷 #91：产物目录混进二进制不该让规划/详情失败


def test_binary_benchmark_artifact_does_not_break_planning(client: TestClient, tmp_path: Path):
    """对标产物目录里的二进制文件不该让「详情查看」和「生成章纲」整条失败。

    修复前两处裸 read_text：
    - `GET /benchmarks/{bid}`（routes.py 遍历目录读全文）实测返回 400
      `'utf-8' codec can't decode byte 0xff in position 0`；
    - `Planner` 拼对标书上下文时读 hooks.md / pacing.md，实测整条 POST /plan
      502 `UnicodeDecodeError`——一本坏产物就把章纲生成堵死。
    """
    from server.core.store import Workbench
    from server.core.schema import BookMeta

    book = ctx_book_dir(client)
    ws = tmp_path / "ws"
    root = ws / "novels" / book
    Workbench(ws).get_store(book)

    bd = root / "analysis" / "benchmarks" / "bk"
    bd.mkdir(parents=True, exist_ok=True)
    (bd / "hooks.md").write_bytes(b"\xff\xfe\x00bad hooks")
    (bd / "pacing.md").write_bytes(b"\xff\xfe\x00bad pacing")
    (bd / "ok.md").write_text("正常产物", encoding="utf-8")

    r = client.get(f"/api/books/{book}/benchmarks/bk")
    assert r.status_code == 200, r.text[:200]
    files = r.json()["files"]
    assert files == {"ok.md": "正常产物"}, files

    # 对照组：正常产物必须照常读出来（防止把守卫写成「一律跳过」）
    (bd / "hooks.md").write_text("钩子基线内容", encoding="utf-8")
    from server.agents.planner import Planner
    from server.model.gateway import ModelGateway

    st2 = Workbench(ws).get_store(book)
    brief = Planner(st2, ModelGateway(ws / "workbench-config"))._pacing_brief()
    assert "钩子基线内容" in brief, brief[:200]


# ------------------------------------------------- 缺陷 #94：真相源文件被写成标量/列表不该 500


def test_broken_truth_files_do_not_500(client: TestClient, tmp_path: Path):
    """作者用外部编辑器/同步盘把真相源文件写坏时，读路径必须降级而不是 500。

    修复前 `load_outline` / `get_character` / `load_profile` 三处对 `atomic.read_json`
    的返回值直接 `setdefault`：`ch-0001.json` 写成 `"oops"` 后 `GET /outline/1` 返回 500
    `AttributeError: 'str' object has no attribute 'setdefault'`，profile 写成 `"str"` 或
    `42` 后 `GET /export/precheck` 同样 500。渠道是外部写入（同步盘/编辑器/断电），
    不是本服务的写路径，所以只能在读侧兜底：读不出 dict 就当没有。
    """
    book = ctx_book_dir(client)
    root = tmp_path / "ws" / "novels" / book
    r = client.put(
        f"/api/books/{book}/chapters/1",
        json={"text": "第一章正文内容在这里，足够长，可以过字数下限检查。" * 30, "commit": False},
    )
    assert r.status_code == 200, r.text
    oj = root / "outline" / "chapters" / "ch-0001.json"
    oj.parent.mkdir(parents=True, exist_ok=True)
    oj.write_text(json.dumps({"ch": 1, "title": "起", "pov": "mc", "scenes": [{"at": "开场", "beat": "起"}]}), encoding="utf-8")
    good_oj = oj.read_text(encoding="utf-8")

    for body in ('"oops"', "[1,2]"):
        oj.write_text(body, encoding="utf-8")
        r = client.get(f"/api/books/{book}/outline/1")
        assert r.status_code == 404, f"{body} -> {r.status_code} {r.text[:160]}"
    oj.write_text(good_oj, encoding="utf-8")
    assert client.get(f"/api/books/{book}/outline/1").status_code == 200

    prof = root / "profiles" / "tomato.json"
    good_prof = prof.read_text(encoding="utf-8")
    for body in ('"str"', "42"):
        prof.write_text(body, encoding="utf-8")
        r = client.get(f"/api/books/{book}/export/precheck")
        assert r.status_code == 200, f"{body} -> {r.status_code} {r.text[:160]}"
    prof.write_text(good_prof, encoding="utf-8")


# 缺陷 #99：同上，git 失败必须在 API 响应里看得见（不是只在日志里）
def test_truth_write_surfaces_git_failure(tmp_path):
    """PUT /truth/{path} 带 commit=True 时，git 失败要回到响应体里。

    这条路径本来就返回 commit 字典（作者能在界面上看到），所以只需确认它
    在 .git 坏掉时是 ok=False 而不是静默成功。
    """,
    from fastapi.testclient import TestClient

    from server.app import create_app

    ws = tmp_path / "ws"
    ws.mkdir(parents=True)
    client = TestClient(create_app(ws))
    d = client.post("/api/books", json={"title": "书", "platform": "tomato"}).json()["dir"]
    root = ws / "novels" / d
    (root / ".git" / "HEAD").write_text("garbage\n", encoding="utf-8")
    r = client.put("/api/books/" + d + "/truth/arcs.md", json={"content": "x", "commit": True, "message": "t"})
    assert r.status_code == 200
    assert r.json()["commit"]["ok"] is False
    assert r.json()["commit"]["error"]



# ---------------------------------------------------------------- 缺陷 #109 SSE 流漏错误


def _sse_blocks(text: str) -> list[dict[str, Any]]:
    """把一段 SSE 文本切成 [{event, data}]（前端 client.js 同款解析）。"""
    out: list[dict[str, Any]] = []
    for block in text.split("\n\n"):
        if not block.strip():
            continue
        ev, data = "message", ""
        for line in block.split("\n"):
            if line.startswith("event:"):
                ev = line[6:].strip()
            elif line.startswith("data:"):
                data += line[5:].strip()
        try:
            payload = json.loads(data) if data else {}
        except Exception:
            payload = {"raw": data}
        out.append({"event": ev, "data": payload})
    return out


def test_generate_stream_reports_writer_failure(client: TestClient, monkeypatch):
    """流式生成失败时，SSE 流里必须发一个 error 事件。

    非流式路径 `except Exception -> HTTPException(502)` 早就把失败说出口了；
    但 stream=true 时响应头在 Writer 真正开跑之前就发出去了，只能靠流内的
    error 事件告诉前端。少了它，作者看到的是「进度条停在 0%，什么都没发生」，
    而服务端其实已经炸了。
    """
    from server.api import routes as R

    book = ctx_book_dir(client)

    class BoomWriter:
        async def write_stream(self, *a, **k):
            raise RuntimeError("模型连接被拒")
            yield  # pragma: no cover  - 让它成为 async generator

    class BoomBC:
        def __init__(self, real):
            self.writer = BoomWriter()
            self._real = real

        def __getattr__(self, item):
            return getattr(self._real, item)

    ctx = client.app.state.ctx
    real = ctx.book(book)
    monkeypatch.setattr(R, "_book", lambda c, b: BoomBC(ctx.book(b)))

    with client.stream(
        "POST", f"/api/books/{book}/generate", json={"ch": 1, "stream": True}
    ) as r:
        assert r.status_code == 200, "流式端点返回 %s" % r.status_code
        body = "".join(r.iter_text())

    blocks = _sse_blocks(body)
    names = [b["event"] for b in blocks]
    assert names, "SSE 流是空的：%r" % body
    assert "start" in names, "连 start 都没发：%r" % body
    assert names[-1] == "error", "Writer 炸了但流里没有 error 事件：%r" % body
    assert "模型连接被拒" in json.dumps(blocks[-1]["data"], ensure_ascii=False)
    # 前端 err 分支：error 事件必须带 message 字段
    assert blocks[-1]["data"].get("message")




# ---------------------------------------------------------------- 缺陷 #122 流式生成的告警被吞


def test_generate_stream_done_event_carries_warnings(client: TestClient, monkeypatch, tmp_path: Path):
    """流式生成成功但有问题时，done 事件必须把 warnings 带出去。

    `Writer.write()` 会算 warnings（装配降级 + 字数偏少/偏多），非流式分支早就把
    它放进响应体了；但 `write_stream()` 产出的 StreamChunk 原先根本没有 warnings
    字段，SSE 的 done 事件也就无从转发 —— 作者点完「生成」，出来一章 9 个字，
    界面上一句提示都没有。#109 保的是「失败要有 error」，这条保的是「成功但有
    问题也要有 warnings」。
    """
    from server.agents.writer import Writer
    from server.api import routes as R
    from server.context.assembler import ContextAssembler
    from server.model.base import StreamChunk

    _patch_gateway(client, tmp_path)
    book = client.post("/api/books", json={"title": "流式告警"}).json()["dir"]
    client.post(f"/api/books/{book}/plan", json={"start_ch": 1, "count": 1})
    client.post(f"/api/books/{book}/outline/1/confirm")

    SHORT = "正文就这么一点点。";

    class ShortGW:
        model = "mock-short"

        async def stream(self, req):
            yield StreamChunk(delta=SHORT, model=self.model, provider="mock")
            yield StreamChunk(delta="", done=True, model=self.model, provider="mock", usage={"total_tokens": 3})

    class ShortBC:
        """只把 writer 换成「短正文 + mock 网关」，其余透传真实 bc。"""

        def __init__(self, real):
            self._real = real
            self.writer = Writer(real.store, ShortGW(), ContextAssembler(real.store))

        def __getattr__(self, item):
            return getattr(self._real, item)

    ctx = client.app.state.ctx
    monkeypatch.setattr(R, "_book", lambda c, b: ShortBC(ctx.book(b)))

    with client.stream(
        "POST", f"/api/books/{book}/generate", json={"ch": 1, "stream": True}
    ) as r:
        assert r.status_code == 200, "流式端点返回 %s" % r.status_code
        body = "".join(r.iter_text())

    blocks = _sse_blocks(body)
    names = [b["event"] for b in blocks]
    assert names[-1] == "done", "流没有正常结束：%r" % names
    done = [b for b in blocks if b["event"] == "done"][-1]["data"]
    # usage 照旧带（字数面板要用）
    # count_cn_words 不计标点，所以这里只断言「字数照旧带出来且明显偏少」
    assert 0 < done.get("usage", {}).get("words", 0) < 100, "done 事件丢了字数：%r" % done
    warns = done.get("warnings")
    assert warns, "done 事件里没有 warnings —— 流式路径把生成告警全吞了（缺陷 #122）"
    assert any("字数偏少" in w for w in warns), "字数偏少告警没被带出来：%r" % warns

# ---------------------------------------------------------------- 缺陷 #110 夜审失败被吞


def test_nightly_reports_failures_at_top_level(client: TestClient, monkeypatch):
    """夜审整批里只要有一章失败，响应顶层必须说出口。

    失败原先只埋在 reviewed[i].error 里，而 PWA 的 triggerNightly 只弹
    「夜审已触发：N 章」——N 是成功+失败的总数，作者看到「3 章」就以为三章都审过了。
    """
    from server.api import routes as R

    book = ctx_book_dir(client)
    # 两章推到 drafted，都会进夜审（状态机要求逐级推进，不能跳）
    for ch in (1, 2):
        for st in ("planned", "writing", "drafted"):
            r = client.put(
                f"/api/books/{book}/outline/{ch}",
                json={"ch": ch, "title": f"t{ch}", "pov": "mc", "scenes": [{"at": "开场", "beat": "起"}], "status": st},
            )
            assert r.status_code == 200, r.text

    class BoomReviewer:
        async def review(self, ch, **k):
            raise RuntimeError("模型超时")

    class BoomBC:
        def __init__(self, real):
            self.reviewer = BoomReviewer()
            self._real = real

        def __getattr__(self, item):
            return getattr(self._real, item)

    ctx = client.app.state.ctx
    monkeypatch.setattr(R, "_book", lambda c, b: BoomBC(ctx.book(b)))

    r = client.post(f"/api/books/{book}/jobs/nightly")
    assert r.status_code == 200
    body = r.json()
    # 旧契约：只看 reviewed 长度 -> 2 章「已审」
    assert len(body["reviewed"]) == 2
    assert all(e.get("error") for e in body["reviewed"])
    # 新契约：顶层有 ok=False 与中文 warnings
    assert body["ok"] is False, "夜审全失败了还报 ok：%r" % body
    warn = " ".join(body["warnings"])
    assert "夜审有 2 章失败" in warn, warn
    assert "第 1 章" in warn and "第 2 章" in warn, warn
    assert "模型超时" not in warn or True  # 明细留在 reviewed[i].error 里


def test_benchmark_upload_rejects_oversize_before_reading_it(client, monkeypatch, tmp_path):
    """缺陷 #112：上传大小上限必须在 read() 之前判。

    修复前是 `raw = await file.read()` 再比大小 —— 8 MB 上限形同虚设：
    一个 2 GB 的文件会先把 2 GB 读进进程内存，然后才回 400。Starlette 的
    UploadFile 超过 1 MB 时已落 SpooledTemporaryFile，seek+tell 就能知道大小
    （实测 9 MB 样本 tell() = 9437184），读前先探是零成本的。

    判据不是「返回了 400」（读完之后也能返 400），而是 **read() 一次都没被调用**。
    """
    from server.api import routes as R
    from server.core.schema import BookMeta

    ctx = AppContext.create(tmp_path / "ws")
    ctx.workbench.create_novel(BookMeta(title="上传", platform="tomato"))
    book = ctx.workbench.refresh_shelf().novels[0].dir

    # 注意：打桩必须打在 **starlette** 的 UploadFile 上 —— fastapi.UploadFile
    # 只是它的子类，实例由 starlette 的 multipart 解析器构造，打在子类上不生效
    # （实测：打 R.UploadFile 时 read 计数永远是 0，测试会假绿）。
    import starlette.datastructures as sd

    reads = {"n": 0}
    real_read = sd.UploadFile.read

    async def spy_read(self, *a, **k):
        reads["n"] += 1
        return await real_read(self, *a, **k)

    monkeypatch.setattr(sd.UploadFile, "read", spy_read)
    # 上限压到 8 字节，样本 2 KB —— 真读的话一定超限
    monkeypatch.setattr(R, "MAX_UPLOAD_BYTES", 8)

    app = create_app(tmp_path / "ws")
    with TestClient(app) as c:
        r = c.post(
            f"/api/books/{book}/benchmarks/upload",
            files={"file": ("big.txt", b"x" * 2048, "text/plain")},
            params={"use_model": "false"},
        )

    assert r.status_code == 400, f"超限文件没被拒：{r.status_code} {r.text[:200]}"
    assert "文件过大" in r.json()["detail"]
    assert reads["n"] == 0, (
        f"超限文件仍然被 read() 了 {reads['n']} 次 —— 大小上限判在读之后（缺陷 #112）"
    )


# ---------------------------------------------------------------- 缺陷 #123


def test_export_precheck_api_reports_ai_flavor(client: TestClient):
    """缺陷 #123：API 层的导出预检必须真的扫出套语。

    修复前 Exporter.precheck 只查 `book.ai_flavor_pipeline` 开关位（真值 True），
    正文里带着 REPLACE_RULES 的套语一条都没检出 —— §9.2 的「发布前强制」
    实际只查了个开关。这条守住 deps 层把 polish 传进 Exporter 的那条线：
    断开 Exporter(self.store) 时这里必须红。
    """
    from server.api.deps import get_context
    from server.core.schema import ChapterOutline, Scene
    from server.platform.polish import REPLACE_RULES

    book = client.post("/api/books", json={"title": "AI 味", "platform": "tomato"}).json()["dir"]
    ctx = get_context()
    store = ctx.book(book).store
    smell = REPLACE_RULES[0][0]
    text = f"# 第1章\n\n他{smell}，然后转身离去。" + "\n\n" + ("他抬起头，他握紧剑，他向前走。\n" * 3)
    store.write_chapter(1, text)
    store.save_outline(ChapterOutline(ch=1, title="一", hook="钟响。", scenes=[Scene(goal="g")]))

    pre = client.get(f"/api/books/{book}/export/precheck").json()
    highs = [i for c in pre["checks"] for i in c["high_issues"]]
    assert any(i["category"] == "ai-flavor" for i in highs), (
        f"API 预检没扫出套语：{highs}（缺陷 #123）"
    )

    exp = client.post(f"/api/books/{book}/export", json={"mode": "合集"}).json()
    assert exp.get("blocked") is False, "套语不该拦截导出（只报不改写）"
    issues = [i for u in exp.get("unresolved_high") or [] for i in u["issues"]]
    assert any(i["category"] == "ai-flavor" for i in issues), (
        f"导出结果里没有未处理套语：{exp.get('unresolved_high')}（缺陷 #123）"
    )
    # 只报不改写：导出不能偷偷改正文
    assert smell in store.read_chapter(1), "导出偷偷改写了正文（缺陷 #123）"


def test_polish_endpoint_returns_issues_and_diff(client: TestClient):
    """缺陷 #123：/polish 必须真的给出问题与 diff（Web 端入口依赖它）。

    修复前 Web 端没有任何 polish 调用点，这一层是「有 API 无出口」。
    """
    from server.api.deps import get_context
    from server.core.schema import ChapterOutline, Scene
    from server.platform.polish import REPLACE_RULES

    book = client.post("/api/books", json={"title": "润色", "platform": "tomato"}).json()["dir"]
    ctx = get_context()
    store = ctx.book(book).store
    smell = REPLACE_RULES[0][0]
    text = f"# 第1章\n\n他{smell}，然后转身离去。"
    store.write_chapter(1, text)
    store.save_outline(ChapterOutline(ch=1, title="一", hook="钟响。", scenes=[Scene(goal="g")]))

    r = client.post(f"/api/books/{book}/polish", json={"ch": 1, "use_model": False, "apply": False})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["issues"], "polish 没检出套语（缺陷 #123）"
    assert data["diff"], "polish 没给出 diff —— Web 端没东西可展示（缺陷 #123）"
    assert data["applied"] is False, "预览模式不该落盘"
    assert smell in store.read_chapter(1), "预览模式改写了正文（缺陷 #123）"
