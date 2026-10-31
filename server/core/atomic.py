"""原子写入与安全读取（§4.3 纪律 1）。

所有落盘必须经这里：写临时文件 → fsync → rename。
防止进程中断产生半截文件，也防止外部编辑器看到写一半的正文。
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any

import yaml


class AtomicWriteError(RuntimeError):
    pass


#: 同一线程已持有的锁文件路径（缺陷 #106）：flock 对同一进程内的第二次
#: LOCK_EX 也会阻塞，嵌套获取会把自己锁死，因此记录线程内已持有的锁。
_LOCK_STATE = threading.local()


def file_lock(path: Path):
    """同一路径的跨进程互斥锁（缺陷 #88）。

    在系统临时目录里用 `moran-lock-<路径哈希>.lock` 上的独占 flock：只覆盖本项目
    自己的进程，不需要外部协同。拿不到锁时退化为不加锁（临时目录不可写 / 非
    POSIX），绝不阻塞写操作。
    """
    import contextlib
    import fcntl

    p = Path(path)
    # 锁文件放在系统临时目录，不落在书里：书目录自带 git，多一个
    # `inspiration.md.lock` 空文件会被 commit_all 一起提交，属于无谓污染。
    import hashlib

    digest = hashlib.sha256(str(p).encode("utf-8")).hexdigest()[:32]
    lock_path = Path(tempfile.gettempdir()) / f"moran-lock-{digest}.lock"

    @contextlib.contextmanager
    def _cm():
        held = getattr(_LOCK_STATE, "held", None)
        if held is None:
            held = set()
            _LOCK_STATE.held = held
        if str(lock_path) in held:
            # 同一线程内嵌套获取：直接使用，不再 flock（否则会把自己锁死）
            yield
            return
        try:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            fh = open(lock_path, "a+")
        except OSError:
            yield
            return
        held.add(str(lock_path))
        try:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            except OSError:
                pass
            yield
        finally:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            held.discard(str(lock_path))
            fh.close()

    return _cm()


def _fsync_dir(directory: Path) -> None:
    """rename 后 fsync 目录，保证目录项落盘（POSIX）。"""
    try:
        fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def write_text_atomic(path: Path, text: str, *, encoding: str = "utf-8") -> Path:
    """原子写文本。父目录自动创建。

    注意：这个函数保证「写入本身」是原子的，但**不**保证「读旧内容 → 拼接 → 写回」
    这个整体是原子的。需要读改写语义的调用方必须用 `file_lock(path)` 把整段包起来
    （缺陷 #88 的教训：20 线程并发速记，响应全 200 但只留下 3–5 条）。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = text.encode(encoding)
    return _write_text_atomic_locked(path, data)


def _write_text_atomic_locked(path: Path, data: bytes) -> Path:
    path = Path(path)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        _fsync_dir(path.parent)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return path


def write_json_atomic(path: Path, obj: Any, *, indent: int = 2, sort_keys: bool = False) -> Path:
    text = json.dumps(obj, ensure_ascii=False, indent=indent, sort_keys=sort_keys)
    return write_text_atomic(path, text + "\n")


def write_yaml_atomic(path: Path, obj: Any) -> Path:
    text = yaml.safe_dump(obj, allow_unicode=True, sort_keys=False, default_flow_style=False)
    return write_text_atomic(path, text)


def append_text(path: Path, text: str, *, header: str = "") -> Path:
    """读旧内容 → 追加 → 原子写回（缺陷 #88）。

    「读旧内容 → 拼接 → 写回」必须整体持锁，否则并发的两个请求各自读到同一份
    旧内容，后写的覆盖先写的：实测 20 个线程并发速记，响应全部 200，文件里
    却只剩 3–5 条 —— 静默丢数据，比报错更糟。

    header：文件不存在时用的初始内容。
    """
    p = Path(path)
    with file_lock(p):
        old = read_text(p, header)
        sep = "" if not old.strip() else ("\n\n" if not old.endswith("\n") else "\n")
        return write_text_atomic(p, old.rstrip() + sep + text.rstrip() + "\n")


def read_text(path: Path, default: str = "") -> str:
    """读文本（缺陷 #64：读不出来就走 default，绝不因为一本半截文件让 API 500）。"""
    p = Path(path)
    if not p.exists():
        return default
    try:
        return p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return default



def read_json(path: Path, default: Any = None) -> Any:
    """读 json。

    缺陷 #64：半截文件必须走 default 而不是抛。外部编辑器/同步盘/git 操作都可能
    在目录里留下写了一半的 json——书页打不开比内容少两条更糟。
    """
    p = Path(path)
    if not p.exists():
        return default
    try:
        raw = p.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return default
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return default



def read_yaml(path: Path, default: Any = None) -> Any:
    """读 yaml（缺陷 #64：半截/非法 yaml 走 default，不抛）。"""
    p = Path(path)
    if not p.exists():
        return default
    try:
        raw = p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return default
    if not raw.strip():
        return default
    try:
        return yaml.safe_load(raw)
    except yaml.YAMLError:
        return default



def append_line_atomic(path: Path, line: str) -> None:
    """jsonl 追加。单行写入 < PIPE_BUF，append 模式本身原子；仍加 fsync。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not line.endswith("\n"):
        line += "\n"
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line)
        fh.flush()
        os.fsync(fh.fileno())


def read_jsonl(path: Path) -> list[dict]:
    """逐行读 JSONL，坏行跳过（保持旧签名）。

    缺陷 #118：静默跳坏行 = 数据静默缩水。要计数请改用 :func:`read_jsonl_report`。
    """
    return read_jsonl_report(path).rows
    


@dataclass
class JsonlReport:
    """``read_jsonl_report()`` 的返回（缺陷 #118）。

    - ``rows``：能解析出来的行
    - ``bad_lines``：解析失败的行号（1 起）
    - ``conflict_markers``：git 合并冲突标记所在行号（文件进版本库，冲突是会发生的）
    - ``unreadable``：文件本身读不出来（不存在 / 权限 / 非 UTF-8 编码）
    """

    rows: list[dict] = field(default_factory=list)
    bad_lines: list[int] = field(default_factory=list)
    conflict_markers: list[int] = field(default_factory=list)
    unreadable: str = ""

    @property
    def ok(self) -> bool:
        return not (self.bad_lines or self.conflict_markers or self.unreadable)

    @property
    def dropped(self) -> int:
        """被丢掉的输入行数（坏行 + 冲突标记行）。"""
        return len(self.bad_lines) + len(self.conflict_markers)

    def problem(self, name: str = "JSONL") -> str:
        """给用户看的一句中文说明；没问题则返回空串（缺陷 #118：不许静默）。"""
        if self.unreadable:
            return f"{name} 读不出来（{self.unreadable}），其中的内容未参与本次计算"
        parts = []
        if self.conflict_markers:
            n = len(self.conflict_markers)
            lines = "、".join(str(i) for i in self.conflict_markers[:5])
            parts.append(f"含 git 冲突标记 {n} 处（第 {lines} 行），请先解决冲突")
        if self.bad_lines:
            n = len(self.bad_lines)
            lines = "、".join(str(i) for i in self.bad_lines[:5])
            parts.append(f"有 {n} 行不是合法 JSON（第 {lines} 行），已跳过")
        if not parts:
            return ""
        return f"{name} " + "；".join(parts)


#: git 合并冲突标记（``memory/entity-deltas.jsonl`` 进版本库，冲突是会发生的）
_CONFLICT_PREFIXES = ("<<<<<<<", "=======", ">>>>>>>")


def read_jsonl_report(path: Path) -> JsonlReport:
    """逐行读 JSONL，并把「读不出来的行」如实报出来（缺陷 #118）。

    旧实现把坏行、冲突标记、整文件编码错乱一律静默跳过，结果实体图谱从 13 个实体
    掉到 4 个，而 ``build_index`` / ``verify`` 依旧全绿——作者以为沉淀了，实际蒸发了。
    这里不改变解析语义（坏行照样跳过，保证索引不会因一行坏数据整体失败），
    但把跳过了什么、跳了多少如实回传，由调用方决定是告警还是报错。
    """
    rep = JsonlReport()
    p = Path(path)
    if not p.exists():
        return rep
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        rep.unreadable = f"{type(exc).__name__}: {exc}"
        return rep
    for i, raw in enumerate(lines, start=1):
        raw = raw.strip()
        if not raw:
            continue
        if raw.startswith(_CONFLICT_PREFIXES):  # 冲突标记不是 JSON，单列出来更好懂
            rep.conflict_markers.append(i)
            continue
        try:
            rep.rows.append(json.loads(raw))
        except json.JSONDecodeError:
            rep.bad_lines.append(i)
            continue
    return rep
