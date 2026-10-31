"""可插拔 embedding（§8.2 降级链）。

设计要点
--------
- **降级是硬要求**：没配置 embedding 时 ``get_embedder(config)`` 返回 ``None``，
  检索自动走纯 BM25，装配流程不中断。任何配置错误都不抛异常，只返回 ``None``。
- ``HashEmbedder`` 是**纯本地兜底**：char-ngram hashing（确定性、无随机源、无网络），
  dim=256。只有显式配置 ``embedding.provider == "hash"`` 才会用它。
- ``OpenAICompatEmbedder`` 走 ``POST {base_url}/v1/embeddings``，
  用 httpx。构造不联网；缺 key 时 ``get_embedder`` 直接返回 ``None``。
- 向量统一 **L2 归一化 + float32 打包**，检索时点积即余弦。

配置形态（``workbench-config/providers.yaml`` 或 dict 直传）::

    embedding:
      provider: hash            # hash | openai_compat | openai | ""
      model: bge-m3
      base_url: http://127.0.0.1:8000
      api_key_env: EMBED_API_KEY   # 或 api_key: sk-xxx
      dim: 1024
      enabled: true
"""
from __future__ import annotations

import asyncio
import hashlib
import math
import os
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Iterable, Mapping, Sequence

import numpy as np

#: 哈希兜底 embedding 的默认维度
HASH_DIM = 256

#: embedding provider 别名 → 规范化 provider
_PROVIDER_ALIASES = {
    "": "",
    "none": "",
    "off": "",
    "disabled": "",
    "hash": "hash",
    "local_hash": "hash",
    "hashing": "hash",
    "openai": "openai_compat",
    "openai_compat": "openai_compat",
    "openai-compat": "openai_compat",
    "compat": "openai_compat",
    "bge": "openai_compat",
    "bge-m3": "openai_compat",
    "tei": "openai_compat",
}

_CJK_MIN = 0x3400
_CJK_MAX = 0x9FFF


# ------------------------------------------------------------------ 基础工具


def run_coro_sync(coro):
    """在同步上下文里跑协程；已有事件循环时退到临时线程。

    ``IndexBuilder.build()`` 是同步 API，而 ``Embedder.embed`` 是异步的，
    所以需要这个桥。
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def normalize_vec(vec: Sequence[float] | np.ndarray) -> np.ndarray:
    """L2 归一化（全零向量原样返回）。"""
    arr = np.asarray(vec, dtype=np.float32).ravel()
    norm = float(np.linalg.norm(arr))
    if norm <= 1e-12:
        return arr
    return (arr / norm).astype(np.float32)


def pack_vector(vec: Sequence[float] | np.ndarray) -> bytes:
    """float32 打包（入库前自动 L2 归一化）。"""
    return normalize_vec(vec).tobytes()


def unpack_vector(blob: bytes) -> np.ndarray:
    """解包成 float32 ndarray。"""
    return np.frombuffer(blob, dtype=np.float32)


def _is_cjk(ch: str) -> bool:
    return _CJK_MIN <= ord(ch) <= _CJK_MAX


# ------------------------------------------------------------------ 接口


class Embedder(ABC):
    """embedding 接口。实现者只需保证 ``embed`` 与 ``dim``。"""

    #: 便于日志/落盘记录的标识
    name: str = "embedder"
    #: 向量维度
    dim: int = 0

    @abstractmethod
    async def embed(self, texts: list[str]) -> list[list[float]]:
        """批量向量化。返回顺序必须与输入一致。"""

    async def embed_query(self, text: str) -> list[float]:
        out = await self.embed([text])
        return out[0] if out else [0.0] * self.dim

    async def embed_one(self, text: str) -> list[float]:
        return await self.embed_query(text)

    def embed_sync(self, texts: list[str]) -> list[list[float]]:
        """同步桥（供 IndexBuilder.build 等同步路径使用）。"""
        return run_coro_sync(self.embed(list(texts)))

    def info(self) -> dict[str, Any]:
        return {"provider": self.name, "dim": int(self.dim)}


# ------------------------------------------------------------------ 本地兜底


class HashEmbedder(Embedder):
    """char-ngram hashing embedding（dim=256，纯本地、确定性、零依赖）。

    做法：中文按 2/3-gram、英文按词切特征，``blake2b`` 定哈希到桶，
    带符号累加后 L2 归一化。语义上远不如 bge-m3，但**永远可用**，
    对同义/近形词的召回也聊胜于无——它是兜底，不是主力。
    """

    name = "hash"

    def __init__(self, dim: int = HASH_DIM):
        self.dim = max(32, int(dim))

    # ---- 特征 ----
    @staticmethod
    def _features(text: str) -> list[str]:
        feats: list[str] = []
        buf: list[str] = []
        word: list[str] = []

        def flush_cjk() -> None:
            if not buf:
                return
            run = "".join(buf)
            feats.extend(run)
            if len(run) > 1:
                feats.extend(run[i : i + 2] for i in range(len(run) - 1))
            if len(run) > 2:
                feats.extend(run[i : i + 3] for i in range(len(run) - 2))
            buf.clear()

        def flush_word() -> None:
            if word:
                feats.append("".join(word).lower())
                word.clear()

        for ch in text:
            if _is_cjk(ch):
                flush_word()
                buf.append(ch)
            elif ch.isalnum():
                flush_cjk()
                word.append(ch)
            else:
                flush_cjk()
                flush_word()
        flush_cjk()
        flush_word()
        return feats

    def _vector(self, text: str) -> np.ndarray:
        vec = np.zeros(self.dim, dtype=np.float32)
        for feat in self._features(text or ""):
            digest = hashlib.blake2b(feat.encode("utf-8"), digest_size=8).digest()
            h = int.from_bytes(digest, "big")
            idx = h % self.dim
            sign = -1.0 if (h >> 63) & 1 else 1.0
            vec[idx] += sign
        return normalize_vec(vec)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(t).tolist() for t in texts]


# ------------------------------------------------------------------ OpenAI 兼容


def _join_url(base_url: str, path: str) -> str:
    base = (base_url or "").rstrip("/")
    if not base:
        return path
    if path.startswith("http://") or path.startswith("https://"):
        return path
    # base 已带 /v1 时不重复拼接
    if base.endswith("/v1") and path.startswith("/v1/"):
        return base + path[len("/v1") :]
    return base + path


class OpenAICompatEmbedder(Embedder):
    """OpenAI 兼容 ``/v1/embeddings`` 客户端（bge-m3 / TEI / vLLM 都能接）。"""

    name = "openai_compat"

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str = "bge-m3",
        dim: int = 1024,
        *,
        timeout: float = 30.0,
        batch_size: int = 32,
        extra_headers: Mapping[str, str] | None = None,
    ):
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.model = model or "bge-m3"
        self.dim = int(dim or 1024)
        self.timeout = float(timeout)
        self.batch_size = max(1, int(batch_size))
        self.extra_headers = dict(extra_headers or {})

    @property
    def url(self) -> str:
        return _join_url(self.base_url, "/v1/embeddings")

    def _headers(self) -> dict[str, str]:
        headers = {"content-type": "application/json"}
        if self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"
        headers.update(self.extra_headers)
        return headers

    def payload(self, texts: list[str]) -> dict[str, Any]:
        return {"model": self.model, "input": list(texts), "encoding_format": "float"}

    async def embed(self, texts: list[str]) -> list[list[float]]:
        import httpx

        out: list[list[float]] = []
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            for i in range(0, len(texts), self.batch_size):
                batch = list(texts[i : i + self.batch_size])
                if not batch:
                    continue
                resp = await client.post(self.url, headers=self._headers(), json=self.payload(batch))
                resp.raise_for_status()
                data = resp.json().get("data") or []
                vecs = [np.asarray(d.get("embedding") or [], dtype=np.float32) for d in data]
                if len(vecs) != len(batch):
                    raise ValueError(f"embeddings 数量不匹配: got {len(vecs)} want {len(batch)}")
                out.extend(v.tolist() for v in vecs)
        return out


# ------------------------------------------------------------------ 构造


def _pluck(config: Any) -> dict[str, Any]:
    """从 Workbench 配置里抠出 embedding 段（容忍 flat / nested 两种形态）。"""
    if config is None:
        return {}
    if isinstance(config, Mapping):
        for key in ("embedding", "embeddings", "embed"):
            sub = config.get(key)
            if isinstance(sub, Mapping):
                return dict(sub)
        if any(k in config for k in ("provider", "model", "api_key", "api_key_env", "base_url", "dim")):
            return dict(config)
        return {}
    # 带属性的配置对象
    for attr in ("embedding", "embeddings"):
        sub = getattr(config, attr, None)
        if sub is not None:
            return _pluck(sub)
    return {}


def _resolve_key(settings: Mapping[str, Any]) -> str:
    key = str(settings.get("api_key") or "").strip()
    if key:
        return key
    env_name = str(settings.get("api_key_env") or "").strip()
    if env_name:
        return str(os.environ.get(env_name) or "").strip()
    return ""


def get_embedder(config: Any = None) -> Embedder | None:
    """按配置构造 embedder。**永不抛异常**：不合规/没配 → ``None``（走纯 BM25）。"""
    try:
        settings = _pluck(config)
        if not settings:
            return None
        if settings.get("enabled") is False:
            return None
        provider = _PROVIDER_ALIASES.get(str(settings.get("provider") or "").strip().lower(), "__unknown__")
        if provider == "":
            return None
        if provider == "hash":
            dim = settings.get("dim") or HASH_DIM
            try:
                dim = int(dim)
            except (TypeError, ValueError):
                dim = HASH_DIM
            return HashEmbedder(dim=dim)
        if provider != "openai_compat":
            return None
        base_url = str(settings.get("base_url") or settings.get("url") or "").strip()
        model = str(settings.get("model") or "").strip()
        if not base_url or not model:
            return None
        api_key = _resolve_key(settings)
        if not api_key:
            # 硬要求：没 key 就当没配置（§8.2 降级链）
            return None
        try:
            dim = int(settings.get("dim") or 1024)
        except (TypeError, ValueError):
            dim = 1024
        return OpenAICompatEmbedder(
            base_url=base_url,
            api_key=api_key,
            model=model,
            dim=dim,
            timeout=float(settings.get("timeout") or 30.0),
            batch_size=int(settings.get("batch_size") or 32),
            extra_headers=settings.get("extra_headers") if isinstance(settings.get("extra_headers"), Mapping) else None,
        )
    except Exception:
        return None


# ------------------------------------------------------------------ 矩阵工具


def cosine_topk(query: Sequence[float], matrix: np.ndarray, k: int) -> list[tuple[int, float]]:
    """行向量矩阵上的点积 top-k（两边都归一化过 → 点积即余弦）。"""
    if matrix.size == 0 or k <= 0:
        return []
    q = normalize_vec(query)
    if q.shape[0] != matrix.shape[1]:
        return []
    scores = matrix @ q
    k = min(k, scores.shape[0])
    idx = np.argpartition(-scores, k - 1)[:k]
    idx = idx[np.argsort(-scores[idx])]
    return [(int(i), float(scores[i])) for i in idx]


def stack_vectors(blobs: Iterable[bytes], dim: int) -> np.ndarray:
    rows = [unpack_vector(b) for b in blobs]
    if not rows:
        return np.zeros((0, max(0, dim)), dtype=np.float32)
    return np.vstack(rows).astype(np.float32)


__all__ = [
    "Embedder",
    "HashEmbedder",
    "OpenAICompatEmbedder",
    "get_embedder",
    "run_coro_sync",
    "normalize_vec",
    "pack_vector",
    "unpack_vector",
    "cosine_topk",
    "stack_vectors",
    "HASH_DIM",
    "_join_url",
    "math",
]
