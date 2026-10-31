"""模型层单测：路由/fallback/熔断/脱敏/JSON 抽取/凭据边界/用量。"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from server.model.base import (
    BadRequest,
    BridgeDown,
    CompletionRequest,
    ContentFiltered,
    HardError,
    Message,
    RateLimited,
    RouteHop,
    RoutingTable,
)
from server.model.gateway import CircuitBreaker, ModelGateway, build_gateway
from server.model.normalizer import (
    desensitize,
    desensitize_request,
    estimate_tokens,
    extract_json,
    normalize_messages,
    normalize_model_name,
)
from server.model.providers.mock import MockProvider
from server.model.providers.registry import (
    DEFAULT_PROVIDERS,
    ProviderConfigError,
    ProviderRegistry,
    ProviderSpec,
    build_registry,
    load_routing,
    parse_route_expr,
    resolve_routing,
    write_default_configs,
)
from server.model.usage import UsageDB


def _req(task: str = "write", content: str = "写第 3 章", **kw) -> CompletionRequest:
    return CompletionRequest(task=task, messages=[Message(role="user", content=content)], **kw)  # type: ignore[arg-type]


def _gateway(tmp_path: Path, *, routing: RoutingTable | None = None) -> ModelGateway:
    reg = ProviderRegistry()
    mock = MockProvider()
    reg.providers["mock-local"] = mock
    reg.specs["mock-local"] = ProviderSpec(name="mock-local", kind="mock", models=["mock-1"])
    table = routing or RoutingTable(routes={"write": [RouteHop("mock-local", "mock-1")], "free": [RouteHop("mock-local", "mock-1")]})
    return ModelGateway(
        tmp_path / "cfg",
        registry=reg,
        routing=table,
        raw_routing={},
        usage_db=UsageDB(tmp_path / "wb.db"),
    )


# ---------------------------------------------------------------- registry


def test_default_configs_written(tmp_path: Path):
    written = write_default_configs(tmp_path / "cfg")
    assert "providers.yaml" in written
    assert (tmp_path / "cfg" / ".env.example").exists()
    assert (tmp_path / "cfg" / ".gitignore").read_text().strip() == ".env"


def test_bridge_must_be_loopback(tmp_path: Path):
    """§7.3 安全红线：订阅桥公网地址必须被拒绝。"""
    import yaml

    cfg = tmp_path / "cfg"
    cfg.mkdir()
    bad = {
        "providers": [
            {"name": "evil", "kind": "openai_compat", "base_url": "https://api.evil-proxy.com", "models": ["x"]}
        ]
    }
    (cfg / "providers.yaml").write_text(yaml.safe_dump(bad), encoding="utf-8")
    with pytest.raises(ProviderConfigError):
        build_registry(cfg / "providers.yaml")


def test_official_api_may_be_public(tmp_path: Path):
    import yaml

    cfg = tmp_path / "cfg"
    cfg.mkdir()
    ok = {
        "providers": [
            {
                "name": "official",
                "kind": "openai_compat",
                "base_url": "https://api.deepseek.com",
                "is_local_bridge": False,
                "models": ["deepseek-chat"],
            }
        ]
    }
    (cfg / "providers.yaml").write_text(yaml.safe_dump(ok), encoding="utf-8")
    reg = build_registry(cfg / "providers.yaml")
    assert "official" in reg.names()


def test_parse_route_expr_arrow():
    hops = parse_route_expr("qoder-ultimate → glm-5.2 → official")
    assert [h.model for h in hops] == ["qoder-ultimate", "glm-5.2", "official"]
    hops2 = parse_route_expr(["a:b", "c:d"])
    assert hops2[0].provider == "a" and hops2[0].model == "b"


def test_resolve_routing_maps_models_to_providers():
    reg = ProviderRegistry()
    for name, models in (("qoder-local", ["qoder/ultimate"]), ("workbuddy-local", ["glm-5.2"])):
        reg.specs[name] = ProviderSpec(name=name, models=models)
        reg.providers[name] = object()
    table = resolve_routing({"routes": {"plan": ["qoder/ultimate", "glm-5.2"]}}, reg)
    assert table.hops("plan")[0].provider == "qoder-local"
    assert table.hops("plan")[1].provider == "workbuddy-local"


def test_routing_unresolved_falls_back_to_mock():
    reg = ProviderRegistry()
    reg.specs["mock-local"] = ProviderSpec(name="mock-local", kind="mock", models=["mock-1"])
    reg.providers["mock-local"] = object()
    table = resolve_routing({"routes": {"plan": ["nonexistent-model"]}}, reg)
    assert table.hops("plan")[0].provider == "mock-local"


# ---------------------------------------------------------------- normalizer


def test_desensitize_replaces_banned_words():
    out = desensitize("血腥的屠杀，尸体遍地")
    assert "血腥" not in out and "屠杀" not in out
    assert "冷硬" in out


def test_desensitize_request_preserves_shape():
    req = _req(content="血腥场面")
    new = desensitize_request(req)
    assert new.task == req.task and new.messages[0].content != req.messages[0].content


def test_normalize_messages_merges_system_for_workbuddy():
    msgs = [
        Message(role="system", content="A"),
        Message(role="system", content="B"),
        Message(role="user", content="hi"),
    ]
    req = CompletionRequest(task="write", messages=msgs)
    out = normalize_messages(req, first_system_only=True)
    assert len(out) == 2 and out[0]["role"] == "system" and "A" in out[0]["content"] and "B" in out[0]["content"]
    # 非 workbuddy 原样
    assert len(normalize_messages(req, first_system_only=False)) == 3


def test_extract_json_handles_fences_and_prose():
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('好的，结果如下：{"a": [1,2,]} 以上。') == {"a": [1, 2]}
    assert extract_json('{"x": {"y": 2}}')["x"]["y"] == 2
    with pytest.raises(ValueError):
        extract_json("完全没有 JSON")


def test_estimate_tokens():
    assert estimate_tokens("你好世界") == 4
    assert estimate_tokens("abcdefgh") == 2
    assert estimate_tokens("") == 0


def test_normalize_model_name_qoder():
    assert normalize_model_name("ultimate", "qoder-local") == "qoder/ultimate"
    assert normalize_model_name("qoder/lite", "qoder-local") == "qoder/lite"


# ---------------------------------------------------------------- gateway


def test_gateway_complete_ok(tmp_path: Path):
    gw = _gateway(tmp_path)
    res = asyncio.run(gw.complete(_req("write", "第 3 章，标题：淬骨")))
    assert res.provider == "mock-local"
    assert "第3章" in res.text or "第 3 章" in res.text
    assert res.total_tokens > 0
    assert gw.usage is not None
    assert gw.usage.usage(days=1)["total"]["calls"] == 1


def test_gateway_rate_limit_opens_circuit_and_falls_through(tmp_path: Path):
    reg = ProviderRegistry()
    bad = MockProvider(fail_times=1, fail_with="rate_limit")
    good = MockProvider()
    for name, prov in (("bad", bad), ("good", good)):
        reg.providers[name] = prov
        reg.specs[name] = ProviderSpec(name=name, kind="mock", models=["m"])
    table = RoutingTable(routes={"write": [RouteHop("bad", "m"), RouteHop("good", "m")]})
    gw = ModelGateway(tmp_path / "cfg", registry=reg, routing=table, raw_routing={})
    res = asyncio.run(gw.complete(_req()))
    assert res.provider == "good"
    assert res.fallback_hops == 1
    assert gw.circuit.is_open("bad")  # 熔断
    # 熔断期间直接跳过 bad
    res2 = asyncio.run(gw.complete(_req()))
    assert res2.provider == "good"


def test_gateway_content_filter_retries_desensitized(tmp_path: Path):
    """content_filter 先脱敏重试，同一 provider 第二次成功。"""
    reg = ProviderRegistry()
    prov = MockProvider(fail_times=1, fail_with="content_filter")
    reg.providers["p"] = prov
    reg.specs["p"] = ProviderSpec(name="p", kind="mock", models=["m"])
    table = RoutingTable(routes={"write": [RouteHop("p", "m")]})
    gw = ModelGateway(tmp_path / "cfg", registry=reg, routing=table, raw_routing={})
    res = asyncio.run(gw.complete(_req(content="血腥屠杀")))
    assert res.provider == "p"
    assert len(prov.calls) == 1


def test_gateway_hard_error_when_all_fail(tmp_path: Path):
    reg = ProviderRegistry()
    for name in ("a", "b"):
        reg.providers[name] = MockProvider(fail_times=10, fail_with="bridge_down")
        reg.specs[name] = ProviderSpec(name=name, kind="mock", models=["m"])
    table = RoutingTable(routes={"write": [RouteHop("a", "m"), RouteHop("b", "m")]})
    gw = ModelGateway(tmp_path / "cfg", registry=reg, routing=table, raw_routing={})
    with pytest.raises(HardError) as ei:
        asyncio.run(gw.complete(_req()))
    assert "不静默降级" in str(ei.value)


def test_write_chain_forces_terminal_hop(tmp_path: Path):
    """write 链禁止 cheap 模型收尾：链尾不是 official/local 时自动补。"""
    reg = ProviderRegistry()
    cheap = MockProvider()
    official = MockProvider()
    reg.providers["cheap"] = cheap
    reg.providers["official"] = official
    reg.specs["cheap"] = ProviderSpec(name="cheap", kind="mock", models=["m"], is_local_bridge=True)
    reg.specs["official"] = ProviderSpec(name="official", kind="mock", models=["m"], is_local_bridge=False)
    table = RoutingTable(
        routes={"write": [RouteHop("cheap", "m")], "free": [RouteHop("official", "m")]}
    )
    gw = ModelGateway(tmp_path / "cfg", registry=reg, routing=table, raw_routing={})
    hops = gw._resolve_hops(_req("write"))
    assert any(not gw.registry.specs[h.provider].is_local_bridge for h in hops)


def test_gateway_stream(tmp_path: Path):
    gw = _gateway(tmp_path)

    async def run():
        chunks = []
        async for c in gw.stream(_req("write", "第 2 章，标题：试锋")):
            chunks.append(c)
        return chunks

    chunks = asyncio.run(run())
    assert chunks and chunks[-1].done
    text = "".join(c.delta for c in chunks)
    assert len(text) > 100
    # 流式也记用量
    assert gw.usage.usage(days=1)["total"]["calls"] == 1


def test_gateway_health(tmp_path: Path):
    gw = _gateway(tmp_path)
    rows = asyncio.run(gw.health())
    assert rows and rows[0]["provider"] == "mock-local" and rows[0]["ok"] is True


def test_gateway_reload_and_describe(tmp_path: Path):
    gw = build_gateway(tmp_path)
    desc = gw.describe()
    assert "providers" in desc and "routes" in desc
    again = gw.reload()
    assert again["routes"]


def test_circuit_breaker_expiry():
    cb = CircuitBreaker(seconds=0.05)
    cb.open("x")
    assert cb.is_open("x")
    import time

    time.sleep(0.06)
    assert not cb.is_open("x")


def test_gateway_complete_json_retries_once(tmp_path: Path):
    gw = _gateway(tmp_path)
    data = asyncio.run(gw.complete_json("curator", "第 5 章"))
    assert "proposals" in data


def test_usage_aggregation(tmp_path: Path):
    db = UsageDB(tmp_path / "u.db")
    from server.core.schema import LlmCallRecord

    for i in range(3):
        db.record(
            LlmCallRecord(
                ts="2026-10-28T10:00:00", task="write", provider="p", model="m", total_tokens=100, cost_yuan=0.1
            )
        )
    db.record(LlmCallRecord(ts="2026-10-28T11:00:00", task="review", provider="q", model="m", ok=False))
    u = db.usage(days=7)
    assert u["total"]["calls"] == 4 and u["total"]["errors"] == 1
    assert u["by_task"]["write"]["calls"] == 3
    assert abs(u["total"]["cost"] - 0.3) < 1e-9


# ---------------------------------------------------------------- mock 正文质量

def test_mock_prose_has_no_massive_sentence_repetition():
    """回归：mock 正文曾把「指节泛白」重复 20 次，demo 看起来像故障。

    约束：同章内任何一个整段都不应出现 3 次以上（允许少量重采样）。
    """
    import collections

    from server.model.base import CompletionRequest, Message
    from server.model.providers.mock import MockProvider

    p = MockProvider()
    req = CompletionRequest(
        task="write",
        messages=[Message(role="user", content="第 7 章 试炼\n标题：试炼\n字数 2000\n结尾钩子：钟响了。")],
    )
    text = asyncio.run(p.complete(req, "mock-1")).text
    paras = [x for x in text.split("\n\n") if x.strip()][1:]  # 去掉标题
    assert len(paras) >= 40, "应产出足量段落"
    counts = collections.Counter(paras)
    worst = max(counts.values())
    assert worst <= 2, f"重复段落最高 {worst} 次：{[k for k, v in counts.items() if v == worst][:2]}"


def test_mock_prose_avoids_banned_ai_phrases():
    """mock 正文不能自带 AIGC 套语，否则审稿规则层会污染回归基线。"""
    from server.model.base import CompletionRequest, Message
    from server.model.providers.mock import MockProvider

    p = MockProvider()
    text = asyncio.run(
        p.complete(
            CompletionRequest(
                task="write",
                messages=[Message(role="user", content="第 2 章 夜行\n标题：夜行\n字数 2000\n结尾钩子：钟响。")],
            ),
            "mock-1",
        )
    ).text
    for bad in ("指节泛白", "仿佛", "宛如", "眼中闪过一丝", "不由自主"):
        assert bad not in text, f"mock 正文不应包含套语: {bad}"


def test_mock_review_only_reports_evidence_in_body():
    """mock 审稿的 evidence 必须能在同一段正文里找到（防 prompt 污染回归）。"""
    from server.model.base import CompletionRequest, Message
    from server.model.providers.mock import MockProvider

    p = MockProvider()
    body = "他把剑收了半寸。矿洞深处滴水声一下一下，像有人在数息。"
    prompt = (
        "第 3 章 审稿\n"
        "文风指纹：忌：眼中闪过一丝、仿佛、宛如。\n"   # 这些出现在 prompt 里，但不在正文里
        f"<<<\n{body}\n>>>"
    )
    out = asyncio.run(
        p.complete(
            CompletionRequest(task="review", json_mode=True, messages=[Message(role="user", content=prompt)]),
            "mock-1",
        )
    ).text
    import json

    from server.agents.reviewer import LITERAL_EVIDENCE_CATEGORIES

    data = json.loads(out)
    for issue in data.get("issues", []):
        # hook / words 这类是「描述性证据」（如「章末」「实际 1848 字」），不要求字面命中；
        # 其余类别必须能在正文里找到原文。
        if issue.get("category") not in LITERAL_EVIDENCE_CATEGORIES:
            continue
        ev = issue.get("evidence", "")
        assert ev[:6] in body, f"evidence 不在正文里: {issue.get('category')} / {ev!r}"


# ---------------------------------------------------------------- 参数解析（防 Markdown 污染）

@pytest.mark.parametrize("n", [1, 2, 4, 7, 12])
def test_mock_planner_honours_requested_count(n: int):
    """回归：prompt 里写的是「生成 **4 章**」，Markdown 粗体曾把数量正则打断，永远只出 3 章。"""
    import json

    from server.model.base import CompletionRequest, Message
    from server.model.providers.mock import MockProvider

    prompt = f"为《测试》（玄幻）生成 **{n} 章**章纲草案，从第 1 章开始（即第 1–{n} 章）。"
    out = asyncio.run(
        MockProvider().complete(
            CompletionRequest(task="plan", json_mode=True, messages=[Message(role="user", content=prompt)]),
            "mock-1",
        )
    ).text
    chaps = json.loads(out)["chapters"]
    assert len(chaps) == n, f"请求 {n} 章，实际 {len(chaps)} 章"
    assert [c["ch"] for c in chaps] == list(range(1, n + 1))


@pytest.mark.parametrize("start", [1, 5, 42])
def test_mock_planner_honours_start_chapter(start: int):
    import json

    from server.model.base import CompletionRequest, Message
    from server.model.providers.mock import MockProvider

    prompt = f"为《测试》生成 **3 章**章纲草案，从第 {start} 章开始（即第 {start}–{start + 2} 章）。"
    out = asyncio.run(
        MockProvider().complete(
            CompletionRequest(task="plan", json_mode=True, messages=[Message(role="user", content=prompt)]),
            "mock-1",
        )
    ).text
    assert [c["ch"] for c in json.loads(out)["chapters"]] == [start, start + 1, start + 2]


def test_mock_planner_titles_and_hooks_unique_in_batch():
    """回归：一批章纲里章名/钩子必须互不相同，否则章纲工作台看起来像坏了。"""
    import json

    from server.model.base import CompletionRequest, Message
    from server.model.providers.mock import MockProvider

    out = asyncio.run(
        MockProvider().complete(
            CompletionRequest(
                task="plan",
                json_mode=True,
                messages=[Message(role="user", content="为《测试》生成 **12 章**章纲草案，从第 1 章开始。")],
            ),
            "mock-1",
        )
    ).text
    chaps = json.loads(out)["chapters"]
    assert len({c["title"] for c in chaps}) == len(chaps)
    assert len({c["hook"] for c in chaps}) == len(chaps)
    for c in chaps:
        assert c["scenes"] and c["scenes"][0]["loc"]
        assert c["hook"].strip()


def test_mock_prose_meets_declared_word_target():
    """回归：mock 只生成 1845 字，而平台下限 2000，导致 demo 每章都预检失败。"""
    from server.core.store import count_cn_words
    from server.model.base import CompletionRequest, Message
    from server.model.providers.mock import MockProvider

    text = asyncio.run(
        MockProvider().complete(
            CompletionRequest(
                task="write",
                messages=[
                    Message(
                        role="user",
                        content="第 3 章 矿脉\n标题：矿脉\n字数 2000–2400（按中文字符计）\n结尾钩子：钟响。",
                    )
                ],
            ),
            "mock-1",
        )
    ).text
    assert count_cn_words(text) >= 2000, f"正文只有 {count_cn_words(text)} 字"


def test_mock_hero_name_is_stable_across_chapters():
    """回归：mock 主角名曾每章重抽（楚澈→沈陌→林轩），一本书读起来像短篇集。"""
    import collections
    import re

    from server.model.base import CompletionRequest, Message
    from server.model.providers.mock import MockProvider

    def hero(text: str) -> str:
        names = re.findall(r"([\u4e00-\u9fff]{2,3})(?=把|没有|抬眼|向前)", text)
        return collections.Counter(names).most_common(1)[0][0]

    m = MockProvider()
    seen = []
    for ch in (1, 2, 3, 5, 8):
        t = asyncio.run(
            m.complete(
                CompletionRequest(
                    task="write",
                    messages=[
                        Message(
                            role="user",
                            content=(
                                f"你是《凡人问道》的执笔者。\n第 {ch} 章 测试\n标题：测试{ch}\n"
                                f"字数 2000–2400（按中文字符计）\n结尾钩子：钟响。"
                            ),
                        )
                    ],
                ),
                "mock-1",
            )
        ).text
        seen.append(hero(t))
    assert len(set(seen)) == 1, f"同一本书主角名应恒定，实际 {seen}"


def test_mock_hero_name_differs_between_books():
    """不同书应该有不同的主角，否则 demo 里两本书像同一本。"""
    import collections
    import re

    from server.model.base import CompletionRequest, Message
    from server.model.providers.mock import MockProvider

    def hero(book: str) -> str:
        t = asyncio.run(
            MockProvider().complete(
                CompletionRequest(
                    task="write",
                    messages=[Message(role="user", content=f"你是《{book}》的执笔者。\n第 1 章 测试\n标题：测试\n字数 2000\n结尾钩子：钟响。")],
                ),
                "mock-1",
            )
        ).text
        names = re.findall(r"([\u4e00-\u9fff]{2,3})(?=把|没有|抬眼|向前)", t)
        return collections.Counter(names).most_common(1)[0][0]

    assert hero("凡人问道") != hero("剑落长安"), "不同书的主角名不应相同"


def test_mock_prose_uses_scene_location_from_outline():
    """回归：章纲写「地点：黑水渡口」，mock 仍写矿洞（完全不读地点）。"""
    from server.model.base import CompletionRequest, Message
    from server.model.providers.mock import MockProvider

    prompt = (
        "你是《凡人问道》的执笔者。\n"
        "### 场景节拍\n1. 地点：world/geography#黑水渡口｜人物：mc\n"
        "   目标：取得旧账册\n"
        "### 本章契约\n- 章号：第 2 章\n- 标题：渡口\n- 字数 2000–2400（按中文字符计）\n"
        "- 结尾钩子：灯笼灭了。\n"
        "**林澈**（mc，protagonist）\n- 性格：外冷内热\n"
    )
    text = asyncio.run(
        MockProvider().complete(
            CompletionRequest(task="write", messages=[Message(role="user", content=prompt)]),
            "mock-1",
        )
    ).text
    # 渡口应该有水/船/岸的意象；不该通篇是矿洞
    assert any(k in text for k in ("渡口", "船", "水", "岸")), "没读到地点，仍是通用池"
    assert text.count("矿洞") <= 2, f"渡口章节不该反复写矿洞（出现 {text.count('矿洞')} 次）"


def test_mock_uses_character_card_name_when_present():
    """角色卡是唯一事实来源：prompt 里有角色卡名时，正文必须用那个名字。"""
    from server.model.base import CompletionRequest, Message
    from server.model.providers.mock import MockProvider

    prompt = (
        "你是《凡人问道》的执笔者。\n第 3 章 测试\n标题：测试\n"
        "字数 2000–2400（按中文字符计）\n结尾钩子：钟响。\n"
        "**林澈**（mc，protagonist）\n- 性格：外冷内热\n"
    )
    text = asyncio.run(
        MockProvider().complete(
            CompletionRequest(task="write", messages=[Message(role="user", content=prompt)]),
            "mock-1",
        )
    ).text
    assert "林澈" in text, "正文应使用角色卡里登记的主角名"


def test_mock_scenery_does_not_leak_across_places():
    """回归：「山门」章节里出现矿道专属句（体感池混了矿洞专用描述）。"""
    from server.model.base import CompletionRequest, Message
    from server.model.providers.mock import SCENERY_BY_PLACE, SENSES_BY_PLACE, MockProvider

    prompt = (
        "你是《凡人问道》的执笔者。\n"
        "### 场景节拍\n1. 地点：world/geography#青云山门｜人物：mc\n"
        "### 本章契约\n- 章号：第 1 章\n- 标题：山门\n- 字数 2000–2400（按中文字符计）\n"
        "- 结尾钩子：钟响。\n"
    )
    text = asyncio.run(
        MockProvider().complete(
            CompletionRequest(task="write", messages=[Message(role="user", content=prompt)]),
            "mock-1",
        )
    ).text

    gate_hits = [p for p in SCENERY_BY_PLACE["gate"] if p.strip("，") in text]
    assert gate_hits, "山门章节应使用 gate 场景池"

    leaked = []
    for kind in ("mine", "ferry", "cliff"):
        for p in SCENERY_BY_PLACE.get(kind, []) + SENSES_BY_PLACE.get(kind, []):
            if p.strip("，") in text:
                leaked.append((kind, p))
    assert not leaked, f"其他地点的专属描写泄漏进山门章节：{leaked[:3]}"


def test_mock_prose_has_single_date_anchor():
    """回归：mock 每章塞 12 处日期句、只有 4 个不同日期，且全在同一个月。

    一章一处时间锚点足够（供 L3 前情衔接与时间线抽取），多了像刷屏。
    """
    import re

    from server.model.base import CompletionRequest, Message
    from server.model.providers.mock import MockProvider

    text = asyncio.run(
        MockProvider().complete(
            CompletionRequest(
                task="write",
                messages=[
                    Message(
                        role="user",
                        content=(
                            "你是《凡人问道》的执笔者。\n第 3 章 测试\n标题：测试\n"
                            "字数 2000–2400（按中文字符计）\n结尾钩子：钟响。\n"
                        ),
                    )
                ],
            ),
            "mock-1",
        )
    ).text
    dates = re.findall(r"青云历\d+年\d+月[\u4e00-\u9fff]+", text)
    assert len(dates) == 1, f"应只有一处时间锚点，实际 {len(dates)} 处：{dates[:4]}"


def _mk_registry():
    from server.model.providers.mock import MockProvider
    from server.model.providers.registry import ProviderRegistry, ProviderSpec

    reg = ProviderRegistry()
    reg.providers["mock-local"] = MockProvider()
    reg.specs["mock-local"] = ProviderSpec(name="mock-local", kind="mock", models=["mock-1"])
    # 廉价第三方桥：is_local_bridge=True → 不可作为链尾
    reg.specs["cheap"] = ProviderSpec(
        name="cheap", kind="openai_compat", models=["m1"], is_local_bridge=True
    )
    return reg


def _hops(tmp_path, reg, routes, tag, task):
    from server.model.base import CompletionRequest, Message, RoutingTable
    from server.model.gateway import ModelGateway
    from server.model.usage import UsageDB

    gw = ModelGateway(
        tmp_path / tag,
        registry=reg,
        routing=RoutingTable(routes=routes),
        raw_routing={},
        usage_db=UsageDB(tmp_path / (tag + ".db")),
    )
    req = CompletionRequest(task=task, messages=[Message(role="user", content="写第 3 章")])
    return gw._resolve_hops(req)


def test_write_chain_appends_terminal_fallback(tmp_path):
    """§7.2 红线：write 链禁止廉价第三方桥收尾。

    _resolve_hops 在末位不是 terminal 时补一个；free/plan 里也挑不出 terminal
    就补硬编码的 mock-local（note="terminal-fallback"）。此前没有任何测试看着这条，
    链尾被悄悄换成廉价模型会导致成章质量静默下降且无人知晓。
    """
    from server.model.base import RouteHop

    reg = _mk_registry()

    # 1) 整条链都是廉价桥 → 硬补 mock-local 收尾
    all_cheap = {
        "write": [RouteHop("cheap", "m1")],
        "free": [RouteHop("cheap", "m1")],
        "plan": [RouteHop("cheap", "m1")],
    }
    hops = _hops(tmp_path, reg, all_cheap, "a", "write")
    assert hops[-1].provider == "mock-local", "廉价桥收尾了: " + str([h.provider for h in hops])
    assert hops[-1].note == "terminal-fallback"

    # 2) free/plan 里有 terminal → 优先用配置里的，不硬编码
    with_free = {
        "write": [RouteHop("cheap", "m1")],
        "free": [RouteHop("mock-local", "mock-1")],
        "plan": [RouteHop("cheap", "m1")],
    }
    hops2 = _hops(tmp_path, reg, with_free, "b", "write")
    assert hops2[-1].provider == "mock-local"
    assert hops2[-1].note == "", "配置里有 terminal 时不应打 terminal-fallback 标记"

    # 3) 末位已经是 terminal → 一个都不补
    already = {
        "write": [RouteHop("cheap", "m1"), RouteHop("mock-local", "mock-1")],
        "free": [RouteHop("cheap", "m1")],
        "plan": [RouteHop("cheap", "m1")],
    }
    assert len(_hops(tmp_path, reg, already, "c", "write")) == 2

    # 4) 非 write 任务不受此红线约束（plan 允许廉价桥收尾）
    assert len(_hops(tmp_path, reg, all_cheap, "d", "plan")) == 1

def test_bridge_missing_base_url_rejected_at_config_time(tmp_path: Path):
    """§7.3 补强：openai_compat 订阅桥缺 base_url 必须在**配置期**就拒绝。

    此前 if spec.base_url and spec.is_local_bridge 会让空地址整条跳过校验，
    配置能正常加载，直到真正发请求才炸 UnsupportedProtocol
    （URL 退化成相对路径 /v1/chat/completions）——失败太晚，且报错词不达意。
    """
    import yaml

    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / "providers.yaml").write_text(
        yaml.safe_dump(
            {"providers": [{"name": "noaddr", "kind": "openai_compat", "models": ["m"]}]}
        ),
        encoding="utf-8",
    )
    with pytest.raises(ProviderConfigError) as ei:
        build_registry(cfg / "providers.yaml")
    assert "base_url" in str(ei.value), f"报错应点名 base_url，实际：{ei.value}"


def test_official_api_may_omit_base_url(tmp_path: Path):
    """对照组：官方 API（is_local_bridge=false）不受回环约束，也不强制 base_url。"""
    import yaml

    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / "providers.yaml").write_text(
        yaml.safe_dump(
            {
                "providers": [
                    {
                        "name": "official",
                        "kind": "openai_compat",
                        "is_local_bridge": False,
                        "models": ["m"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    reg = build_registry(cfg / "providers.yaml")
    assert reg.specs["official"].is_local_bridge is False


# ------------------------------------------------- 缺陷 #95：模型吐长串符号时解析卡死


def test_extract_json_is_bounded_on_garbage_input():
    """`extract_json` 的括号平衡扫描必须封顶，不能让一次模型输出把请求卡住。

    修复前 `s.find("{", start + 1)` 逐个花括号重试，最坏 O(n^2)：实测 1 万个 `{`
    要 2.2s、5 万个要 53s。而 `Gateway.complete_json` 失败后还会**再重试一遍**
    （gateway.py:230），等于一次调用能卡近两分钟。模型抽风吐长串符号完全可能，
    这不是理论风险。
    现在：超 `MAX_JSON_SCAN_CHARS`(20000) 直接拒；长度合格但结构离谱时按
    `MAX_JSON_SCAN_STEPS`(200000) 步预算拒。正常响应远小于这两个数。
    """
    from server.model.normalizer import extract_json
    import time

    # 各种正常/容忍写法必须仍然解析得出来（封顶不能误伤）
    assert extract_json('{"a": 1}')["a"] == 1
    assert extract_json("```json" + chr(10) + '{"a": 1}' + chr(10) + "```")["a"] == 1
    assert extract_json("好的，结果如下：" + chr(10) + '{"title": "雪夜"}')["title"] == "雪夜"
    assert extract_json('{"a": 1, "b": [1,2,]}')["b"] == [1, 2]
    assert extract_json('{"a": "不是}结尾{"}')["a"] == "不是}结尾{"
    assert extract_json("[1,2,3]") == {"data": [1, 2, 3]}

    # 长串垃圾必须秒拒，且仍然是可预期的 ValueError（走重试链，不 500）
    for blob in ("{" * 19000, "{" * 21000, "{" * 1000 + "}" * 1000):
        t0 = time.time()
        try:
            extract_json(blob)
            raise AssertionError("长串垃圾应当被拒绝")
        except ValueError:
            pass
        assert time.time() - t0 < 2.0, f"{len(blob)} 字符解析耗时 {time.time() - t0:.1f}s，仍是 O(n^2)"


# ------------------------------------------------- 缺陷 #96：超长路由表达式触发正则回溯


def test_route_expr_is_capped():
    """`parse_route_expr` 的 `\s*(?:→|->|>)\s*` 两侧都是 `\s*`，纯空白输入会反复回溯。

    实测 2 万个空格 1.3s、2 万个换行 1.3s——路由表达式来自作者手写的 routing.yaml，
    手滑粘一长串空白/换行完全可能，服务启动解析配置时就会卡住。
    超 `MAX_ROUTE_EXPR_CHARS`(2000) 直接抛 `ProviderConfigError`（配置期报错，
    不是请求期卡顿）。正常表达式不受影响。
    """
    from server.model.providers.registry import MAX_ROUTE_EXPR_CHARS, parse_route_expr
    import time

    assert len(parse_route_expr("mock-local:mock-1")) == 1
    assert len(parse_route_expr("a -> b")) == 2
    assert len(parse_route_expr("a → b")) == 2
    assert parse_route_expr(["a:b", "c:d"])[1].model == "d"

    for blob in (" " * (MAX_ROUTE_EXPR_CHARS + 1), "\n" * (MAX_ROUTE_EXPR_CHARS + 1)):
        t0 = time.time()
        try:
            parse_route_expr(blob)
            raise AssertionError("超长路由表达式应当被拒绝")
        except ProviderConfigError:
            pass
        assert time.time() - t0 < 1.0, f"{len(blob)} 字符解析耗时 {time.time() - t0:.1f}s"
