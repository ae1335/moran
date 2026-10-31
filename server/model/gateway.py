"""ModelGateway：路由 + fallback + 熔断 + 用量（§7.2）。

关键语义（实现强制）：
- 429 → 熔断该桥 10 分钟，切 hop
- 流式中断 → 抛错，由上层按章幂等重生成
- content_filter → 先脱敏重试，再切 hop
- write 链禁止 cheap 模型收尾 → 链尾必须是 official/local
- 全部失败 → HardError，不静默降级
"""
from __future__ import annotations

import asyncio
import datetime as dt
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator

from ..core.schema import LlmCallRecord
from .base import (
    BadRequest,
    BridgeDown,
    CompletionRequest,
    CompletionResult,
    ContentFiltered,
    HardError,
    ModelError,
    RateLimited,
    RouteHop,
    RoutingTable,
    StreamChunk,
    TASK_TEMPERATURE,
    Message,
)
from .normalizer import desensitize_request, estimate_tokens, normalize_model_name
from .providers.registry import (
    ProviderRegistry,
    build_registry,
    load_routing,
    write_default_configs,
)
from .usage import UsageDB


@dataclass
class CircuitBreaker:
    """429 熔断该桥 N 秒（§7.2）。"""

    seconds: float = 600.0
    _until: dict[str, float] = field(default_factory=dict)

    def open(self, provider: str) -> None:
        self._until[provider] = time.time() + self.seconds

    def is_open(self, provider: str) -> bool:
        until = self._until.get(provider, 0.0)
        if until and time.time() < until:
            return True
        if until and time.time() >= until:
            self._until.pop(provider, None)
        return False

    def remaining(self, provider: str) -> float:
        return max(0.0, self._until.get(provider, 0.0) - time.time())

    def reset(self, provider: str | None = None) -> None:
        if provider:
            self._until.pop(provider, None)
        else:
            self._until.clear()


class ModelGateway:
    """所有 Agent 的唯一模型入口。"""

    def __init__(
        self,
        config_dir: Path,
        *,
        usage_db: UsageDB | None = None,
        registry: ProviderRegistry | None = None,
        routing: RoutingTable | None = None,
        raw_routing: dict[str, Any] | None = None,
        circuit_seconds: float = 600.0,
    ):
        self.config_dir = Path(config_dir)
        write_default_configs(self.config_dir)
        self.registry = registry or build_registry(
            self.config_dir / "providers.yaml", self.config_dir / ".env"
        )
        if routing is None:
            routing, raw = load_routing(self.config_dir / "routing.yaml", self.registry)
            self.raw_routing = raw
        else:
            self.raw_routing = raw_routing or {}
        self.routing = routing
        self.circuit = CircuitBreaker(seconds=circuit_seconds)
        self.usage = usage_db
        self._lock = asyncio.Lock()

    # ---------------- 配置热重载 ----------------
    def reload(self) -> dict[str, Any]:
        """POST /api/routing/reload：不重启进程换配置。"""
        self.registry = build_registry(self.config_dir / "providers.yaml", self.config_dir / ".env")
        routing, raw = load_routing(self.config_dir / "routing.yaml", self.registry)
        self.routing = routing
        self.raw_routing = raw
        return self.describe()

    def describe(self) -> dict[str, Any]:
        return {
            "providers": [
                {
                    "name": s.name,
                    "kind": s.kind,
                    "bridge": s.bridge,
                    "base_url": s.base_url,
                    "models": s.models,
                    "enabled": s.enabled,
                    "is_local_bridge": s.is_local_bridge,
                    "note": s.note,
                    "circuit_open": self.circuit.is_open(s.name),
                    "circuit_remaining_s": round(self.circuit.remaining(s.name), 1),
                }
                for s in self.registry.specs.values()
            ],
            "routes": {
                task: [{"provider": h.provider, "model": h.model} for h in hops]
                for task, hops in self.routing.routes.items()
            },
        }

    # ---------------- 主入口 ----------------
    async def complete(self, req: CompletionRequest) -> CompletionResult:
        hops = self._resolve_hops(req)
        if not hops:
            raise HardError(f"任务 {req.task} 没有可用路由")

        errors: list[str] = []
        hop_index = 0
        for hop in hops:
            provider = self.registry.get(hop.provider)
            if provider is None:
                errors.append(f"{hop.provider}: 未注册")
                continue
            spec = self.registry.specs.get(hop.provider)
            if spec and not spec.enabled:
                errors.append(f"{hop.provider}: 已禁用")
                continue
            if self.circuit.is_open(hop.provider):
                errors.append(f"{hop.provider}: 熔断中({self.circuit.remaining(hop.provider):.0f}s)")
                continue

            attempt_req = req
            for sanitized in (False, True):
                try:
                    res = await provider.complete(attempt_req, hop.model)
                    # 以注册表里的 canonical 名为准（provider 实例名可能与注册名不同，如复用 mock）
                    res.provider = hop.provider
                    if hop.model and hop.model != "*":
                        res.model = hop.model
                    res.bridge = getattr(provider, "bridge", "") or res.bridge
                    res.fallback_hops = hop_index
                    if not res.total_tokens:
                        res.prompt_tokens = sum(estimate_tokens(m.content) for m in attempt_req.messages)
                        res.completion_tokens = estimate_tokens(res.text)
                        res.total_tokens = res.prompt_tokens + res.completion_tokens
                    self._log(req, res)
                    return res
                except ContentFiltered as exc:
                    self._incident(hop.provider, "content_filter", str(exc))
                    if not sanitized:
                        attempt_req = desensitize_request(req)  # 先脱敏重试
                        continue
                    errors.append(f"{attempt_req.task}@{hop.provider}: {exc}")
                    break
                except RateLimited as exc:
                    self.circuit.open(hop.provider)
                    self._incident(hop.provider, "rate_limit", str(exc))
                    errors.append(f"{hop.provider}: 限流熔断")
                    self._log_failure(req, hop, exc)
                    break
                except BadRequest as exc:
                    self._incident(hop.provider, "bad_request", str(exc))
                    errors.append(f"{hop.provider}: {exc}")
                    self._log_failure(req, hop, exc)
                    break
                except BridgeDown as exc:
                    self._incident(hop.provider, "bridge_down", str(exc))
                    errors.append(f"{hop.provider}: {exc}")
                    self._log_failure(req, hop, exc)
                    break
                except ModelError as exc:
                    self._incident(hop.provider, "model_error", str(exc))
                    errors.append(f"{hop.provider}: {exc}")
                    self._log_failure(req, hop, exc)
                    break
            hop_index += 1

        raise HardError(
            f"任务 {req.task} 全部路由失败（不静默降级）。尝试: " + " | ".join(errors)
        )

    async def complete_text(self, task: str, prompt: str, *, system: str = "", **kw) -> str:
        msgs: list[Message] = []
        if system:
            msgs.append(Message(role="system", content=system))
        msgs.append(Message(role="user", content=prompt))
        req = CompletionRequest(task=task, messages=msgs, **kw)  # type: ignore[arg-type]
        res = await self.complete(req)
        return res.text

    async def complete_json(self, task: str, prompt: str, *, system: str = "", **kw) -> dict[str, Any]:
        from .normalizer import extract_json

        msgs: list[Message] = []
        if system:
            msgs.append(Message(role="system", content=system))
        msgs.append(Message(role="user", content=prompt))
        req = CompletionRequest(task=task, messages=msgs, json_mode=True, **kw)  # type: ignore[arg-type]
        res = await self.complete(req)
        try:
            return extract_json(res.text)
        except ValueError:
            # 结构化任务解析失败：用同一 provider 再来一次（一次，不循环）
            retry_req = CompletionRequest(
                task=task,
                messages=msgs
                + [
                    Message(role="assistant", content=res.text[:2000]),
                    Message(role="user", content="只输出合法 JSON，不要任何解释或 Markdown 围栏。"),
                ],
                json_mode=True,
                chapter=kw.get("chapter"),
                temperature=0.0,
            )
            res2 = await self.complete(retry_req)
            return extract_json(res2.text)

    async def stream(self, req: CompletionRequest) -> AsyncIterator[StreamChunk]:
        hops = self._resolve_hops(req)
        if not hops:
            raise HardError(f"任务 {req.task} 没有可用路由")

        errors: list[str] = []
        for hop in hops:
            provider = self.registry.get(hop.provider)
            spec = self.registry.specs.get(hop.provider)
            if provider is None or (spec and not spec.enabled) or self.circuit.is_open(hop.provider):
                continue
            produced = 0
            started = time.monotonic()
            try:
                async for chunk in provider.stream(req, hop.model):
                    if chunk.delta:
                        produced += len(chunk.delta)
                    if chunk.done:
                        usage = chunk.usage or {}
                        prompt_tokens = usage.get("prompt_tokens") or sum(
                            estimate_tokens(m.content) for m in req.messages
                        )
                        completion_tokens = usage.get("completion_tokens") or produced
                        record = LlmCallRecord(
                            ts=_now(),
                            task=req.task,
                            provider=hop.provider,
                            model=normalize_model_name(chunk.model or hop.model, hop.provider),
                            bridge=getattr(provider, "bridge", ""),
                            prompt_tokens=prompt_tokens,
                            completion_tokens=completion_tokens,
                            total_tokens=usage.get("total_tokens") or prompt_tokens + completion_tokens,
                            latency_ms=int((time.monotonic() - started) * 1000),
                            ok=True,
                            chapter=req.chapter,
                            cost_yuan=self.registry.cost(hop.provider, prompt_tokens, completion_tokens),
                            stream=True,
                        )
                        if self.usage:
                            self.usage.record(record)
                    yield chunk
                return
            except ContentFiltered as exc:
                self._incident(hop.provider, "content_filter", str(exc))
                errors.append(f"{hop.provider}: 内容拦截")
                continue
            except RateLimited as exc:
                self.circuit.open(hop.provider)
                self._incident(hop.provider, "rate_limit", str(exc))
                errors.append(f"{hop.provider}: 限流熔断")
                continue
            except (BridgeDown, ModelError) as exc:
                self._incident(hop.provider, "stream_error", str(exc))
                errors.append(f"{hop.provider}: {exc}")
                # 流式中断：已产出内容作废，上层按章幂等重生成（§7.2）
                continue

        raise HardError(f"任务 {req.task} 流式全部路由失败: " + " | ".join(errors))

    # ---------------- 健康 ----------------
    async def health(self, *, probe: bool = True) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for name, spec in self.registry.specs.items():
            prov = self.registry.get(name)
            ok, detail = (True, "disabled") if not spec.enabled else (False, "unavailable")
            if spec.enabled and probe and prov is not None:
                try:
                    ok, detail = await asyncio.wait_for(prov.health(), timeout=6.0)
                except asyncio.TimeoutError:
                    ok, detail = False, "health timeout"
                except Exception as exc:
                    ok, detail = False, f"{type(exc).__name__}: {exc}"
            if self.usage and spec.enabled:
                self.usage.record_health(name, ok, detail)
            out.append(
                {
                    "provider": name,
                    "bridge": spec.bridge,
                    "base_url": spec.base_url,
                    "enabled": spec.enabled,
                    "ok": ok,
                    "detail": detail,
                    "circuit_open": self.circuit.is_open(name),
                    "models": spec.models,
                }
            )
        return out

    # ---------------- 内部 ----------------
    def _resolve_hops(self, req: CompletionRequest) -> list[RouteHop]:
        if req.force_provider:
            return [RouteHop(provider=req.force_provider, model=req.force_model or "*")]
        hops = list(self.routing.hops(req.task))
        # write 链禁止 cheap 模型收尾（§7.2）
        if req.task == "write" and hops:
            last = hops[-1]
            if not _is_terminal_hop(last, self.registry):
                for cand in self.routing.hops("free") + self.routing.hops("plan"):
                    if _is_terminal_hop(cand, self.registry) and cand not in hops:
                        hops.append(cand)
                        break
                else:
                    hops.append(RouteHop(provider="mock-local", model="mock-1", note="terminal-fallback"))
        return hops

    def _log(self, req: CompletionRequest, res: CompletionResult) -> None:
        if not self.usage:
            return
        rec = LlmCallRecord(
            ts=_now(),
            task=req.task,
            provider=res.provider,
            model=normalize_model_name(res.model, res.provider),
            bridge=res.bridge,
            prompt_tokens=res.prompt_tokens,
            completion_tokens=res.completion_tokens,
            total_tokens=res.total_tokens,
            latency_ms=res.latency_ms,
            ok=True,
            chapter=req.chapter,
            cost_yuan=self.registry.cost(res.provider, res.prompt_tokens, res.completion_tokens),
            fallback_hops=res.fallback_hops,
            stream=req.stream,
        )
        self.usage.record(rec)

    def _log_failure(self, req: CompletionRequest, hop: RouteHop, exc: ModelError) -> None:
        if not self.usage:
            return
        self.usage.record(
            LlmCallRecord(
                ts=_now(),
                task=req.task,
                provider=hop.provider,
                model=hop.model,
                ok=False,
                error=f"{type(exc).__name__}: {exc}"[:500],
                chapter=req.chapter,
                stream=req.stream,
            )
        )

    def _incident(self, provider: str, kind: str, detail: str) -> None:
        if self.usage:
            self.usage.record_incident(provider, kind, detail[:500])


def _is_terminal_hop(hop: RouteHop, reg: ProviderRegistry) -> bool:
    """链尾必须是 official（收费直连）或 local（本地 mock/桥），不能是廉价第三方。"""
    if hop.provider in ("mock-local",):
        return True
    spec = reg.specs.get(hop.provider)
    if spec is None:
        return False
    return not spec.is_local_bridge  # official API


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


# ---------------------------------------------------------------- 便捷构造


def build_gateway(root: Path, *, with_usage: bool = True) -> ModelGateway:
    """工作区根 → Gateway。db 在 workbench/workbench.db。"""
    config_dir = Path(root) / "workbench-config"
    usage = UsageDB(Path(root) / "workbench" / "workbench.db") if with_usage else None
    return ModelGateway(config_dir, usage_db=usage)
