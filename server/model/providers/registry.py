"""Provider 注册表 + 配置加载（§7.1）。

providers.yaml 只存引用名，密钥走 .env（§7.3 凭据不入 git）。
桥地址强制 127.0.0.1（Pydantic 校验拒绝公网地址）。
"""
from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .openai_compat import OpenAICompatProvider
from .mock import MockProvider
from ..base import RouteHop, RoutingTable

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}


class ProviderConfigError(ValueError):
    pass


def _assert_loopback(url: str, name: str) -> None:
    """§7.3：桥必须 127.0.0.1。公网地址一律拒绝（防止把订阅凭据发给第三方中转）。"""
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if not host:
        raise ProviderConfigError(f"provider {name}: base_url 无 host: {url}")
    if host in LOCAL_HOSTS:
        return
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        raise ProviderConfigError(
            f"provider {name}: 桥地址必须为回环地址（127.0.0.1/localhost），当前 {host}。"
            " 订阅桥禁止走公网/中转站（§7.3 安全红线）。"
        )
    if not ip.is_loopback:
        raise ProviderConfigError(
            f"provider {name}: 桥地址必须为回环地址，当前 {host}。订阅桥禁止走公网/中转站（§7.3）。"
        )


class ProviderSpec(BaseModel):
    """providers.yaml 里的一条。"""

    model_config = ConfigDict(extra="allow")

    name: str
    kind: str = "openai_compat"  # openai_compat | mock
    base_url: str = ""
    bridge: str = ""
    api_key_env: str = ""
    api_key: str = ""
    models: list[str] = Field(default_factory=list)
    timeout_s: float = 300.0
    health_path: str = "/v1/models"
    first_system_only: bool = False
    supports_json_mode: bool = True
    supports_stream: bool = True
    cost_per_1k_prompt: float = 0.0
    cost_per_1k_completion: float = 0.0
    extra_headers: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True
    note: str = ""
    is_local_bridge: bool = True

    @field_validator("base_url")
    @classmethod
    def _check_url(cls, v: str, info) -> str:
        if not v:
            return v
        # mock 不需要地址
        if info.data.get("kind") == "mock":
            return v
        return v


def load_dotenv(path: Path) -> dict[str, str]:
    """极简 .env 解析（不引新依赖）。"""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        k, _, v = line.partition("=")
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        out[k.strip()] = v
    return out


# ---------------------------------------------------------------- 默认配置


DEFAULT_PROVIDERS: dict[str, Any] = {
    "providers": [
        {
            "name": "mock-local",
            "kind": "mock",
            "bridge": "mock",
            "models": ["mock-1"],
            "enabled": True,
            "note": "离线兜底：无 Key / 无网时保证流水线可跑通",
            "is_local_bridge": True,
        },
        {
            "name": "workbuddy-local",
            "kind": "openai_compat",
            "base_url": "http://127.0.0.1:8787",
            "bridge": "workbuddy2api",
            "api_key_env": "WORKBUDDY_API_KEY",
            "models": ["glm-5.2", "kimi-k2.7", "deepseek-v4-pro"],
            "first_system_only": True,
            "enabled": True,
            "note": "国际版需首条 system；审核拦截→脱敏重试",
        },
        {
            "name": "qoder-local",
            "kind": "openai_compat",
            "base_url": "http://127.0.0.1:8963",
            "bridge": "qoder2api",
            "api_key_env": "QODER_API_KEY",
            "health_path": "/v1/models",
            "models": ["qoder/ultimate", "qoder/lite"],
            "enabled": True,
            "note": "模型名用档位 key；无 /health，用 /v1/models 探活",
        },
        {
            "name": "trae-local",
            "kind": "openai_compat",
            "base_url": "http://127.0.0.1:19950",
            "bridge": "trae2api",
            "api_key_env": "TRAE_API_KEY",
            "models": ["pro", "gpt-5.4"],
            "enabled": True,
            "note": "双区按模型名自动选区；4001=区域错配不重试",
        },
        {
            "name": "deepseek-official",
            "kind": "openai_compat",
            "base_url": "https://api.deepseek.com",
            "bridge": "",
            "api_key_env": "DEEPSEEK_API_KEY",
            "models": ["deepseek-chat"],
            "is_local_bridge": False,
            "cost_per_1k_prompt": 0.001,
            "cost_per_1k_completion": 0.002,
            "enabled": True,
            "note": "官方兜底，per_token 计费",
        },
    ]
}

DEFAULT_ROUTING: dict[str, Any] = {
    "routes": {
        "plan": ["qoder-local:qoder/ultimate", "workbuddy-local:glm-5.2", "deepseek-official:deepseek-chat"],
        "write": ["trae-local:pro", "workbuddy-local:deepseek-v4-pro", "deepseek-official:deepseek-chat"],
        "review": ["workbuddy-local:kimi-k2.7", "deepseek-official:deepseek-chat"],
        "polish": ["qoder-local:qoder/lite", "trae-local:pro"],
        "curator": ["workbuddy-local:kimi-k2.7", "deepseek-official:deepseek-chat"],
        "summarize": ["workbuddy-local:kimi-k2.7", "deepseek-official:deepseek-chat"],
        "deconstruct": ["workbuddy-local:glm-5.2", "deepseek-official:deepseek-chat"],
        "free": ["workbuddy-local:glm-5.2", "deepseek-official:deepseek-chat"],
    },
    # 链尾语义（§7.2）：write 链禁止 cheap 模型收尾 → 必含 official/local
    "require_terminal": {"write": True},
    # 429 熔断时长（秒）
    "circuit_seconds": 600,
}


# ---------------------------------------------------------------- 注册表


@dataclass
class ProviderRegistry:
    providers: dict[str, Any] = field(default_factory=dict)
    specs: dict[str, ProviderSpec] = field(default_factory=dict)

    def get(self, name: str):
        return self.providers.get(name)

    def names(self) -> list[str]:
        return sorted(self.providers)

    def enabled_names(self) -> list[str]:
        return [n for n, s in self.specs.items() if s.enabled and n in self.providers]

    def cost(self, provider: str, prompt_tokens: int, completion_tokens: int) -> float:
        spec = self.specs.get(provider)
        if spec is None:
            return 0.0
        return (
            prompt_tokens / 1000.0 * spec.cost_per_1k_prompt
            + completion_tokens / 1000.0 * spec.cost_per_1k_completion
        )


def _env_value(spec: ProviderSpec, env: dict[str, str]) -> str:
    if spec.api_key:
        return spec.api_key
    if spec.api_key_env:
        return env.get(spec.api_key_env) or os.environ.get(spec.api_key_env, "")
    return ""


def build_registry(config_path: Path, env_path: Path | None = None) -> ProviderRegistry:
    """从 providers.yaml 构建注册表。文件不存在时用默认配置（但不写盘）。"""
    env = load_dotenv(env_path) if env_path else {}
    raw: dict[str, Any]
    if config_path.exists():
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        # 缺陷 #98：yaml 顶层写成标量/列表时 raw.get 抛 AttributeError，服务起不来。
        # 配置错了必须大声说人话，不能静默回退默认——那等于假装作者没配过。
        if not isinstance(raw, dict):
            raise ProviderConfigError(
                f"providers.yaml 格式不对：顶层必须是键值表，当前是 {type(raw).__name__}。"
                f"请检查 {config_path}（常见原因：列表项少了缩进）"
            )
    else:
        raw = DEFAULT_PROVIDERS

    entries = raw.get("providers") or []
    reg = ProviderRegistry()
    for item in entries:
        spec = ProviderSpec.model_validate(item)
        if spec.kind == "mock":
            prov = MockProvider(name=spec.name, bridge=spec.bridge or "mock")
        else:
            # §7.3：只有「订阅桥」强制回环；官方 API（is_local_bridge=false）允许公网
            # openai_compat 必须有地址：空 base_url 会让请求变成相对路径
            # （/v1/chat/completions），到请求时才炸 UnsupportedProtocol——
            # 配置期就该拒绝，别等到写正文那一步才失败。
            if spec.is_local_bridge:
                if not spec.base_url:
                    raise ProviderConfigError(
                        f"provider {spec.name}: kind=openai_compat 缺少 base_url。"
                        "订阅桥必须指向本地回环地址（如 http://127.0.0.1:8787）。"
                    )
                _assert_loopback(spec.base_url, spec.name)
            prov = OpenAICompatProvider(
                name=spec.name,
                base_url=spec.base_url,
                bridge=spec.bridge,
                api_key=_env_value(spec, env),
                models=spec.models,
                timeout_s=spec.timeout_s,
                extra_headers=spec.extra_headers,
                health_path=spec.health_path,
                first_system_only=spec.first_system_only,
                supports_json_mode=spec.supports_json_mode,
                supports_stream=spec.supports_stream,
            )
        reg.providers[spec.name] = prov
        reg.specs[spec.name] = spec
    return reg


#: 缺陷 #96：路由表达式来自作者手写的 routing.yaml，超长就拒绝解析（防爆破性回溯）。
MAX_ROUTE_EXPR_CHARS = 2_000


def parse_route_expr(expr: str) -> list[RouteHop]:
    """'qoder-ultimate → glm-5.2 → official' 或 ['a:b', 'c:d'] 形态。

    规则：
    - 'provider:model' → 精确
    - 只有 model 名 → 在 providers 里找声明了该 model 的第一个（保持声明顺序）
    - 只有 provider 名 → 该 provider 的第一个 model
    """
    hops: list[RouteHop] = []
    if isinstance(expr, str):
        # 缺陷 #96：两侧都是 `\s*` 的 split 在纯空白输入上会反复回溯
        # （实测 2 万个空格 1.3s、2 万个换行 1.3s）。路由表达式来自作者手写的
        # routing.yaml，手滑粘一长串空白完全可能，超长直接拒绝解析。
        if len(expr) > MAX_ROUTE_EXPR_CHARS:
            raise ProviderConfigError(
                f"路由表达式过长：{len(expr)} 字符（上限 {MAX_ROUTE_EXPR_CHARS}）：{expr[:80]}"
            )
        parts = re.split(r"\s*(?:→|->|>)\s*", expr)
    else:
        parts = list(expr)
    for p in parts:
        p = str(p).strip()
        if not p:
            continue
        if ":" in p:
            prov, _, model = p.partition(":")
            hops.append(RouteHop(provider=prov.strip(), model=model.strip()))
        else:
            hops.append(RouteHop(provider="", model=p, note="resolve"))
    return hops


def resolve_routing(raw: dict[str, Any], reg: ProviderRegistry) -> RoutingTable:
    """把 routing.yaml 的表达式解析成可执行的 (provider, model) 链。"""
    routes_raw = raw.get("routes") or raw
    table = RoutingTable()
    for task, expr in routes_raw.items():
        if task.startswith("_") or not isinstance(expr, (str, list)):
            continue
        hops = parse_route_expr(expr)
        resolved: list[RouteHop] = []
        for hop in hops:
            if hop.provider:
                resolved.append(hop)
                continue
            # 只给了 model：在 provider 里找
            target = hop.model
            found: RouteHop | None = None
            for pname, spec in reg.specs.items():
                if not spec.enabled:
                    continue
                if target in spec.models:
                    found = RouteHop(provider=pname, model=target)
                    break
            if found is None:
                # 也可能给的是 provider 名
                if target in reg.specs and reg.specs[target].models:
                    found = RouteHop(provider=target, model=reg.specs[target].models[0])
            if found is None:
                # 兜底到 mock，保证链不空（真实运行时会记录一条告警）
                found = RouteHop(provider="mock-local", model="mock-1", note=f"unresolved:{target}")
            resolved.append(found)
        table.routes[task] = resolved
    if "free" not in table.routes and "plan" in table.routes:
        table.routes["free"] = list(table.routes["plan"])
    return table


def load_routing(path: Path, reg: ProviderRegistry) -> tuple[RoutingTable, dict[str, Any]]:
    if path.exists():
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        # 缺陷 #98：同 build_registry——顶层非字典时给中文报错，否则起服务即崩。
        if not isinstance(raw, dict):
            raise ProviderConfigError(
                f"routing.yaml 格式不对：顶层必须是键值表，当前是 {type(raw).__name__}。"
                f"请检查 {path}（常见原因：tasks 下少了缩进）"
            )
    else:
        raw = DEFAULT_ROUTING
    return resolve_routing(raw, reg), raw


def write_default_configs(config_dir: Path, *, overwrite: bool = False) -> list[str]:
    """生成 providers.yaml / routing.yaml / .env.example。返回实际写入的文件名。"""
    config_dir.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    files = {
        "providers.yaml": DEFAULT_PROVIDERS,
        "routing.yaml": DEFAULT_ROUTING,
    }
    for name, obj in files.items():
        f = config_dir / name
        if f.exists() and not overwrite:
            continue
        header = (
            "# 订阅桥 + 官方 API 统一注册。密钥只写环境变量名，真实值放 .env（§7.3）\n"
            "# 桥地址必须是回环地址，公网地址会被拒绝\n"
        )
        f.write_text(header + yaml.safe_dump(obj, allow_unicode=True, sort_keys=False), encoding="utf-8")
        written.append(name)

    env_example = config_dir / ".env.example"
    if not env_example.exists() or overwrite:
        env_example.write_text(
            "# 复制为 .env 并填入真实值；.env 已在 .gitignore 中\n"
            "WORKBUDDY_API_KEY=\n"
            "QODER_API_KEY=\n"
            "TRAE_API_KEY=\n"
            "DEEPSEEK_API_KEY=\n"
            "# embedding（可选，不配则检索降级为纯 BM25）\n"
            "EMBEDDING_BASE_URL=\n"
            "EMBEDDING_API_KEY=\n"
            "EMBEDDING_MODEL=bge-m3\n"
            "RERANKER_BASE_URL=\n"
            "RERANKER_API_KEY=\n",
            encoding="utf-8",
        )
        written.append(".env.example")

    gi = config_dir / ".gitignore"
    if not gi.exists():
        gi.write_text(".env\n", encoding="utf-8")
        written.append(".gitignore")
    return written
