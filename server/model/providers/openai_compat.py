"""OpenAI 兼容 provider（订阅桥 + 官方 API 统一适配）。

三家桥（workbuddy2api / qoder2api / trae2api）与官方 API 都是 OpenAI 兼容协议，
差异集中在 normalizer 里处理；这里只管协议收发。
"""
from __future__ import annotations

import json
import time
from typing import Any, AsyncIterator

import httpx

from ..base import (
    BadRequest,
    BridgeDown,
    CompletionRequest,
    CompletionResult,
    ContentFiltered,
    ModelError,
    RateLimited,
    StreamChunk,
)


class OpenAICompatProvider:
    """一个 base_url + api_key 的 OpenAI 兼容端点。"""

    def __init__(
        self,
        name: str,
        base_url: str,
        *,
        bridge: str = "",
        api_key: str = "",
        models: list[str] | None = None,
        timeout_s: float = 300.0,
        extra_headers: dict[str, str] | None = None,
        health_path: str = "/v1/models",
        first_system_only: bool = False,
        json_mode_field: str = "response_format",
        supports_json_mode: bool = True,
        supports_stream: bool = True,
    ):
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.bridge = bridge or name
        self.api_key = api_key
        self.models = list(models or [])
        self.timeout_s = timeout_s
        self.extra_headers = dict(extra_headers or {})
        self.health_path = health_path
        # workbuddy 国际版需要把 system 放在首条（normalizer 使用）
        self.first_system_only = first_system_only
        self.json_mode_field = json_mode_field
        self.supports_json_mode = supports_json_mode
        self.supports_stream = supports_stream

    # ---------------- 内部 ----------------
    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        h.update(self.extra_headers)
        return h

    def _payload(self, req: CompletionRequest, model: str) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": model,
            "messages": [m.to_dict() for m in req.messages],
            "temperature": req.temperature if req.temperature is not None else 0.7,
            "max_tokens": req.max_tokens,
            "stream": req.stream,
        }
        if req.json_mode and self.supports_json_mode:
            body[self.json_mode_field] = {"type": "json_object"}
        if req.stream:
            body["stream_options"] = {"include_usage": True}
        return body

    def _classify(self, status: int, body: str, model: str) -> ModelError:
        snippet = (body or "")[:600]
        lowered = snippet.lower()
        if status == 429:
            return RateLimited(f"{self.name} 限流(429): {snippet}", status=429, provider=self.name, model=model)
        if status in (400, 422):
            # 内容审核 / 区域错配都走 400 系
            if any(k in snippet for k in ("content_filter", "content_policy", "sensitive", "审核", "违规")):
                return ContentFiltered(f"{self.name} 内容拦截: {snippet}", status=status, provider=self.name, model=model)
            if "4001" in snippet or "region" in lowered:
                return BadRequest(f"{self.name} 区域/参数错配(不重试): {snippet}", status=status, provider=self.name, model=model)
            return BadRequest(f"{self.name} 请求非法: {snippet}", status=status, provider=self.name, model=model)
        if status in (401, 403):
            return BadRequest(f"{self.name} 鉴权失败: {snippet}", status=status, provider=self.name, model=model)
        if status == 404:
            return BadRequest(f"{self.name} 端点/模型不存在: {snippet}", status=status, provider=self.name, model=model)
        if status >= 500:
            return BridgeDown(f"{self.name} 桥/上游故障({status}): {snippet}", status=status, provider=self.name, model=model)
        return ModelError(f"{self.name} HTTP {status}: {snippet}", status=status, provider=self.name, model=model)

    # ---------------- 非流式 ----------------
    async def complete(self, req: CompletionRequest, model: str) -> CompletionResult:
        url = f"{self.base_url}/v1/chat/completions"
        started = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=req.timeout_s or self.timeout_s) as client:
                resp = await client.post(url, headers=self._headers(), json=self._payload(req, model))
        except httpx.TimeoutException as exc:
            raise BridgeDown(f"{self.name} 超时: {exc}", provider=self.name, model=model) from exc
        except httpx.HTTPError as exc:
            raise BridgeDown(f"{self.name} 连接失败: {exc}", provider=self.name, model=model) from exc

        if resp.status_code >= 400:
            raise self._classify(resp.status_code, resp.text, model)

        try:
            data = resp.json()
        except json.JSONDecodeError as exc:
            raise BridgeDown(f"{self.name} 返回非 JSON: {resp.text[:200]}", provider=self.name, model=model) from exc

        return self._to_result(data, model, int((time.monotonic() - started) * 1000))

    def _to_result(self, data: dict[str, Any], model: str, latency_ms: int) -> CompletionResult:
        choices = data.get("choices") or []
        text = ""
        finish = ""
        if choices:
            msg = choices[0].get("message") or {}
            text = msg.get("content") or choices[0].get("text") or ""
            finish = choices[0].get("finish_reason") or ""
        usage = data.get("usage") or {}
        return CompletionResult(
            text=text,
            provider=self.name,
            model=data.get("model") or model,
            bridge=self.bridge,
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            total_tokens=int(usage.get("total_tokens") or 0)
            or int(usage.get("prompt_tokens") or 0) + int(usage.get("completion_tokens") or 0),
            latency_ms=latency_ms,
            finish_reason=finish,
            raw=data,
        )

    # ---------------- 流式 ----------------
    async def stream(self, req: CompletionRequest, model: str) -> AsyncIterator[StreamChunk]:
        if not self.supports_stream:
            res = await self.complete(req, model)
            yield StreamChunk(delta=res.text, done=False, provider=self.name, model=res.model)
            yield StreamChunk(
                delta="",
                done=True,
                provider=self.name,
                model=res.model,
                usage={
                    "prompt_tokens": res.prompt_tokens,
                    "completion_tokens": res.completion_tokens,
                    "total_tokens": res.total_tokens,
                },
            )
            return

        url = f"{self.base_url}/v1/chat/completions"
        payload = self._payload(req, model)
        usage: dict[str, int] = {}
        try:
            async with httpx.AsyncClient(timeout=req.timeout_s or self.timeout_s) as client:
                async with client.stream("POST", url, headers=self._headers(), json=payload) as resp:
                    if resp.status_code >= 400:
                        body = (await resp.aread()).decode("utf-8", "replace")
                        raise self._classify(resp.status_code, body, model)
                    async for line in resp.aiter_lines():
                        if not line:
                            continue
                        if line.startswith(":"):
                            continue
                        if not line.startswith("data:"):
                            continue
                        chunk = line[5:].strip()
                        if chunk == "[DONE]":
                            break
                        try:
                            obj = json.loads(chunk)
                        except json.JSONDecodeError:
                            continue
                        if obj.get("usage"):
                            u = obj["usage"]
                            usage = {
                                "prompt_tokens": int(u.get("prompt_tokens") or 0),
                                "completion_tokens": int(u.get("completion_tokens") or 0),
                                "total_tokens": int(u.get("total_tokens") or 0),
                            }
                        for ch in obj.get("choices") or []:
                            delta = ch.get("delta") or {}
                            piece = delta.get("content")
                            if piece:
                                yield StreamChunk(delta=piece, done=False, provider=self.name, model=model)
        except httpx.TimeoutException as exc:
            raise BridgeDown(f"{self.name} 流式超时: {exc}", provider=self.name, model=model) from exc
        except httpx.HTTPError as exc:
            raise BridgeDown(f"{self.name} 流式连接失败: {exc}", provider=self.name, model=model) from exc

        yield StreamChunk(delta="", done=True, provider=self.name, model=model, usage=usage)

    # ---------------- 健康 ----------------
    async def health(self) -> tuple[bool, str]:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{self.base_url}{self.health_path}", headers=self._headers())
            if resp.status_code < 500:
                return True, f"HTTP {resp.status_code}"
            return False, f"HTTP {resp.status_code}"
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"

    def supports(self, model: str) -> bool:
        return not self.models or model in self.models or model == "*"

    def __repr__(self) -> str:  # pragma: no cover
        return f"<OpenAICompatProvider {self.name} {self.base_url}>"
