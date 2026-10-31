"""三桥差异归一化（§7.1 差异修正列）。

不同桥对 OpenAI 协议的偏离集中在这里抹平，provider 只负责收发。
"""
from __future__ import annotations

import json
import re
from typing import Any

from .base import CompletionRequest, Message

# 审核拦截的常见替身写法（脱敏重试时替换成安全词）
#: 缺陷 #95：`extract_json` 的括号平衡扫描是 O(n^2)，实测 5 万个 `{` 要 53s。
#: 超出这个长度直接拒绝，不再进入扫描（正常 JSON 响应远小于此）。
MAX_JSON_SCAN_CHARS = 20_000
#: 扫描步数总预算（O(n) 上限）：括号平衡扫描本身最坏 O(n^2)，
#: 只封长度挡不住「长度合格但全是 `{`」的抽风输出。
MAX_JSON_SCAN_STEPS = 200_000

DESENSITIZE_MAP: list[tuple[str, str]] = [
    (r"血\s*腥", "冷硬"),
    (r"屠\s*杀", "压制"),
    (r"尸\s*体", "残躯"),
    (r"死\s*亡", "终局"),
    (r"碎\s*尸", "溃散"),
    (r"割\s*喉", "制住"),
    (r"自\s*尽", "决然"),
]


def desensitize(text: str) -> str:
    """审核拦截后的脱敏重试（§7.2：content_filter 先脱敏重试再切 hop）。"""
    out = text
    for pat, rep in DESENSITIZE_MAP:
        out = re.sub(pat, rep, out)
    return out


def desensitize_request(req: CompletionRequest) -> CompletionRequest:
    return CompletionRequest(
        task=req.task,
        messages=[Message(role=m.role, content=desensitize(m.content)) for m in req.messages],
        temperature=req.temperature,
        max_tokens=req.max_tokens,
        json_mode=req.json_mode,
        stream=req.stream,
        chapter=req.chapter,
        force_provider=req.force_provider,
        force_model=req.force_model,
        timeout_s=req.timeout_s,
    )


def normalize_messages(req: CompletionRequest, *, first_system_only: bool) -> list[dict[str, str]]:
    """workbuddy 国际版要求首条是 system，且只接受一条 system。

    其它 provider 原样透传。
    """
    msgs = [m.to_dict() for m in req.messages]
    if not first_system_only:
        return msgs
    systems = [m for m in msgs if m["role"] == "system"]
    others = [m for m in msgs if m["role"] != "system"]
    if not systems:
        return others
    merged = "\n\n".join(m["content"] for m in systems)
    return [{"role": "system", "content": merged}, *others]


def extract_json(text: str) -> dict[str, Any]:
    """从模型输出里抠出 JSON（容忍 ```json 围栏、前后废话、尾逗号）。"""
    if not text:
        raise ValueError("空响应，无法解析 JSON")
    s = text.strip()
    # 去围栏
    m = re.search(r"```(?:json)?\s*(.*?)\s*```", s, re.S)
    if m:
        s = m.group(1).strip()
    # 直接尝试
    try:
        obj = json.loads(s)
        return obj if isinstance(obj, dict) else {"data": obj}
    except json.JSONDecodeError:
        pass
    # 抠第一个平衡的 {...}
    # 缺陷 #95：原先 `s.find("{", start + 1)` 逐个花括号重试，最坏 O(n^2)。
    # 实测 1 万个 `{` 要 2.2s、5 万个要 53s，而模型抽风吐出长串符号完全可能——
    # 一次 complete_json 就能把请求卡住近一分钟（还是双份：失败后会再重试一遍）。
    if len(s) > MAX_JSON_SCAN_CHARS:
        raise ValueError(f"响应过长，无法解析 JSON（{len(s)} 字符，上限 {MAX_JSON_SCAN_CHARS}）: {text[:200]}")
    start = s.find("{")
    steps = 0
    while start != -1:
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(s)):
            steps += 1
            if steps > MAX_JSON_SCAN_STEPS:
                raise ValueError(
                    f"响应里的 JSON 结构过深/过长，放弃解析（扫描超过 {MAX_JSON_SCAN_STEPS} 步）: {text[:200]}"
                )
            ch = s[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidate = s[start : i + 1]
                    candidate = re.sub(r",(\s*[}\]])", r"\1", candidate)  # 去尾逗号
                    try:
                        obj = json.loads(candidate)
                        return obj if isinstance(obj, dict) else {"data": obj}
                    except json.JSONDecodeError:
                        break
        start = s.find("{", start + 1)
    raise ValueError(f"响应中找不到合法 JSON: {text[:200]}")


def normalize_model_name(model: str, provider: str) -> str:
    """qoder 用档位 key；trae 双区；统一去掉前缀噪声，便于用量统计聚合。"""
    m = model.strip()
    if provider.startswith("qoder") and not m.startswith("qoder/"):
        m = f"qoder/{m}" if m in ("ultimate", "lite") else m
    return m


def estimate_tokens(text: str) -> int:
    """粗略 token 估算：中文按 1 字 ≈ 1 token，英文按 4 字符 ≈ 1 token。

    仅在 provider 不返回 usage 时使用（订阅桥常不返回）。
    """
    if not text:
        return 0
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
    other = len(text) - cjk
    return cjk + (max(1, other // 4) if other else 0)
