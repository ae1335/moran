"""架构铁律单测：README §架构「依赖方向单向，禁止反向依赖」。

README.md:79 声明 `api → agents → context/model → core` 单向，**禁止反向依赖**。
这条此前零测试——靠人看代码守。一旦有人让 core 反过来 import agents，
分层就塌了（core 会依赖模型层，离线运行、单测隔离全受影响），而且不会有任何测试变红。
本文件用 AST 扫全量 import，把这条承诺变成可执行事实。
"""
from __future__ import annotations

import ast
import pathlib
import re
from pathlib import Path

# 从低到高：低层不许 import 高层
LAYER_ORDER = ["core", "model", "context", "agents", "platform", "api"]
SERVER_ROOT = pathlib.Path(__file__).resolve().parent.parent / "server"


def _layer_of(parts: list[str]) -> str | None:
    for p in parts:
        if p in LAYER_ORDER:
            return p
    return None


def _resolve(src_parts: list[str], node: ast.AST) -> list[str] | None:
    """把 import 语句解析成目标模块的绝对 parts（处理相对导入）。"""
    if isinstance(node, ast.ImportFrom):
        if not node.module:
            return None
        if node.level and node.level > 0:
            base = list(src_parts[:-1])  # 模块所在包
            for _ in range(node.level - 1):
                if base:
                    base.pop()
            return base + node.module.split(".")
        return node.module.split(".")
    if isinstance(node, ast.Import):
        names = [a.name.split(".") for a in node.names if a.name.startswith("server")]
        return names[0] if len(names) == 1 else None
    return None


def _collect_reverse() -> list[tuple[str, int, str]]:
    """返回 (源文件:行号, 目标层, 目标模块) 的反向依赖列表。"""
    bad: list[tuple[str, int, str]] = []
    for f in SERVER_ROOT.rglob("*.py"):
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        rel = f.relative_to(SERVER_ROOT.parent).with_suffix("")
        src_parts = list(rel.parts)
        src_layer = _layer_of(src_parts)
        if src_layer is None:
            continue
        for n in ast.walk(tree):
            if not isinstance(n, (ast.Import, ast.ImportFrom)):
                continue
            parts = _resolve(src_parts, n)
            if not parts or parts[0] != "server":
                continue
            tgt_layer = _layer_of(parts[1:])
            if tgt_layer is None or tgt_layer == src_layer:
                continue
            if LAYER_ORDER.index(tgt_layer) > LAYER_ORDER.index(src_layer):
                bad.append((f"{rel.as_posix()}:{n.lineno}", tgt_layer, ".".join(parts)))
    return bad


def test_no_reverse_dependency_between_layers():
    """铁律：低层不得 import 高层（api → agents → context/model → core）。"""
    bad = _collect_reverse()
    assert not bad, "发现反向依赖（低层 import 高层）：\n" + "\n".join(
        f"  {loc}  ->  {layer}  ({mod})" for loc, layer, mod in bad
    )


def test_layer_scan_actually_sees_files():
    """自检：扫描器不能因为路径写错而「零违反」——那是最危险的假绿。"""
    files = list(SERVER_ROOT.rglob("*.py"))
    assert len(files) > 20, f"server/ 下 py 文件太少，扫描路径可能错了：{len(files)}"
    # api 层确实存在且被识别
    assert (SERVER_ROOT / "api" / "routes.py").exists()

# ---------------------------------------------------------------- 缺陷 #69
# 文档里的「单测清单」以前是手抄的：实际 352 项，文档顶着 305 项，
# 还漏记了 test_derivative / test_architecture / test_scheduler 三个文件。
# 手抄必然漂移，所以让测试反过来盯着文档。
DOC_VERIFICATION = pathlib.Path(__file__).resolve().parent.parent / "docs" / "VERIFICATION.md"


def _doc_rows() -> list[tuple[str, int]]:
    """从文档 §1 表格里解析出 (文件名, 文档自称的条数)。"""
    out: list[tuple[str, int]] = []
    for line in DOC_VERIFICATION.read_text(encoding="utf-8").splitlines():
        m = re.match(r"\|\s*`tests/([A-Za-z0-9_]+\.py)`\s*\((\d+)\)\s*\|", line)
        if m:
            out.append((m.group(1), int(m.group(2))))
    return out


def _real_counts() -> dict[str, int]:
    """AST 数每个测试文件里真正的 test_ 函数条数（不跑 pytest，快）。"""
    counts: dict[str, int] = {}
    for p in sorted((pathlib.Path(__file__).resolve().parent).glob("test_*.py")):
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - 语法坏了自己就是收集错误
            continue
        n = 0
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
                n += 1
        counts[p.name] = n
    return counts


def test_doc_lists_every_test_file():
    """文档漏记测试文件 = 漏报覆盖范围，必须红。"""
    doc = {name for name, _ in _doc_rows()}
    real = set(_real_counts())
    missing = sorted(real - doc)
    assert not missing, f"docs/VERIFICATION.md §1 漏记了这些测试文件: {missing}"
    extra = sorted(doc - real)
    assert not extra, f"docs/VERIFICATION.md §1 记了不存在的文件: {extra}"


def test_doc_test_counts_match_reality():
    """文档自称的条数必须等于文件里真的有几条 test_* 函数。"""
    real = _real_counts()
    bad = [f"{name}: 文档写 {n} 条，实际 {real.get(name)} 条" for name, n in _doc_rows() if real.get(name) != n]
    assert not bad, "docs/VERIFICATION.md §1 的条数与代码不符: " + "; ".join(bad)


def test_doc_headline_total_matches_reality():
    """一句话结论里的总数必须等于实际总数，不能停留在一个旧数字上。"""
    total = sum(_real_counts().values())
    text = DOC_VERIFICATION.read_text(encoding="utf-8")
    m = re.search(r"单测\s*(\d+)\s*条用例", text)
    assert m, "docs/VERIFICATION.md 的一句话结论里找不到「单测 N 条用例」"
    assert int(m.group(1)) == total, f"一句话结论写 {m.group(1)} 条，实际 {total} 条"
