"""模型层公共契约（§7）。先冻结接口，实现可替换。

上层（agents）只依赖这里的 Protocol / dataclass，不依赖任何具体 provider。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Literal, Protocol, runtime_checkable

TaskName = Literal["plan", "write", "review", "polish", "curator", "deconstruct", "summarize", "free"]

# 任务级默认参数
TASK_TEMPERATURE: dict[str, float] = {
    "plan": 0.8,
    "write": 0.85,
    "review": 0.2,
    "polish": 0.6,
    "curator": 0.1,
    "deconstruct": 0.3,
    "summarize": 0.3,
    "free": 0.7,
}


@dataclass
class Message:
    role: Literal["system", "user", "assistant"]
    content: str

    def to_dict(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


@dataclass
class CompletionRequest:
    task: TaskName
    messages: list[Message]
    temperature: float | None = None
    max_tokens: int = 4096
    json_mode: bool = False
    stream: bool = False
    chapter: int | None = None
    # 显式指定 provider/model（覆盖路由），用于「重试换模型」
    force_provider: str | None = None
    force_model: str | None = None
    timeout_s: float = 300.0


@dataclass
class CompletionResult:
    text: str
    provider: str
    model: str
    bridge: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    latency_ms: int = 0
    fallback_hops: int = 0
    finish_reason: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class StreamChunk:
    delta: str
    done: bool = False
    provider: str = ""
    model: str = ""
    error: str = ""
    usage: dict[str, int] = field(default_factory=dict)
    # 缺陷 #122：流式生成原先只能报「成功/失败」，字数偏少、检索降级这类告警
    # 在 StreamChunk 里根本没有字段可带，SSE 的 done 事件也就无从转发 ——
    # 作者点完「生成」，出来一章 9 个字却一句提示都没有。
    warnings: list[str] = field(default_factory=list)


class ModelError(RuntimeError):
    """所有 provider 错误的基类。"""

    retryable = False

    def __init__(self, message: str, *, status: int | None = None, provider: str = "", model: str = ""):
        super().__init__(message)
        self.status = status
        self.provider = provider
        self.model = model


class RateLimited(ModelError):
    """429 / 限流：熔断该桥 10 分钟。"""

    retryable = True


class ContentFiltered(ModelError):
    """内容审核拦截：先脱敏重试，再切 hop。"""

    retryable = True


class BridgeDown(ModelError):
    """桥进程不可达 / 5xx：切 hop。"""

    retryable = True


class BadRequest(ModelError):
    """400 类不可重试（如区域错配 4001）。"""

    retryable = False


class HardError(ModelError):
    """整条 fallback 链耗尽：不静默降级。"""

    retryable = False


@runtime_checkable
class LLMProvider(Protocol):
    """Provider 协议。实现放在 server/model/providers/ 下。"""

    name: str
    bridge: str

    async def complete(self, req: CompletionRequest, model: str) -> CompletionResult: ...

    def stream(self, req: CompletionRequest, model: str) -> AsyncIterator[StreamChunk]: ...

    async def health(self) -> tuple[bool, str]: ...

    def supports(self, model: str) -> bool: ...


@dataclass
class RouteHop:
    provider: str
    model: str
    note: str = ""


@dataclass
class RoutingTable:
    """routing.yaml 的内存形态：task -> [hop, ...]"""

    routes: dict[str, list[RouteHop]] = field(default_factory=dict)

    def hops(self, task: str) -> list[RouteHop]:
        return self.routes.get(task) or self.routes.get("free") or []


@dataclass
class ProviderHealth:
    provider: str
    ok: bool
    detail: str = ""
    checked_at: str = ""
    circuit_open_until: float = 0.0
