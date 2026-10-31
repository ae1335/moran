"""git 自动化（§4.3 纪律 3）。

- 章状态 → final：commit `ch-0042: 淬骨 (2,143字)` + tag `ch/0042`
- truth/ 每次确认写入：单独 commit
- 时光机 = git log + tag 直接查询
"""
from __future__ import annotations

import logging
import os
from . import atomic
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import git as _git  # GitPython
    from git.exc import GitCommandError, InvalidGitRepositoryError, NoSuchPathError
except Exception:  # pragma: no cover - GitPython 缺失时降级为不可用
    _git = None  # type: ignore

    class GitCommandError(Exception):  # type: ignore
        pass

    class InvalidGitRepositoryError(Exception):  # type: ignore
        pass

    class NoSuchPathError(Exception):  # type: ignore
        pass


DEFAULT_GITIGNORE = """\
# 可重建数据不入 git（§4.3 纪律 4）
.rag-index/
exports/
*.tmp
.*.tmp
__pycache__/
*.pyc
.DS_Store
"""


@dataclass
class CommitResult:
    ok: bool
    sha: str = ""
    message: str = ""
    tag: str = ""
    error: str = ""
    #: 缺陷 #126：commit 成功但 tag 没打上时写原因，且 ``tag`` 必须置空。
    #: 以前照抄 ``tag=ch/0042``，作者以为时光机能查到，实际点进去是空的。
    tag_error: str = ""


logger = logging.getLogger("workbench.git")


class GitBot:
    """一本书的 git 门面。所有操作幂等、失败不抛（返回 CommitResult）。"""

    def __init__(self, root: Path, *, author_name: str = "novel-workbench", author_email: str = "workbench@local"):
        self.root = Path(root)
        self.author_name = author_name
        self.author_email = author_email

    def _git_lock(self):
        """同一书库的 git 操作互斥锁（缺陷 #106）。

        git 的 index.lock / COMMIT_EDITMSG / logs/HEAD.lock 都不允许并发：并发
        提交时 GitPython 会抛 `Unable to create .git/index.lock: File exists`
        或 `Reference at refs/heads/master does already exist`。实测 12 个
        线程并发 commit_all 有 9 个失败，而它们各自的文件其实都已落盘 ——
        于是「真相源写进去了但没进版本库」，时光机上查不到。

        锁落在 .git 目录（GitBot 自己的锁），不落在书内容目录，
        因此不会被 commit_all 提交。
        """
        return atomic.file_lock(self.root / ".git")

    # ---------------- 基础 ----------------
    @property
    def available(self) -> bool:
        return _git is not None

    def _repo(self) -> "Any":
        if _git is None:
            raise GitCommandError("GitPython 未安装")
        try:
            return _git.Repo(self.root)
        except (InvalidGitRepositoryError, NoSuchPathError):
            return None

    def is_repo(self) -> bool:
        return self._repo() is not None

    def init(self) -> CommitResult:
        if _git is None:
            return CommitResult(False, error="GitPython 未安装")
        self.root.mkdir(parents=True, exist_ok=True)
        gi = self.root / ".gitignore"
        if not gi.exists():
            atomic.write_text_atomic(gi, DEFAULT_GITIGNORE)
        try:
            repo = self._repo()
            if repo is None:
                repo = _git.Repo.init(self.root)
            with repo.config_writer() as cw:
                cw.set_value("user", "name", self.author_name)
                cw.set_value("user", "email", self.author_email)
            return CommitResult(True, message="initialized")
        except Exception as exc:
            logger.warning("git 仓库初始化失败：%s -> %s", self.root, exc)
            return CommitResult(False, error=str(exc))

    def _ensure(self) -> "Any":
        repo = self._repo()
        if repo is None:
            self.init()
            repo = self._repo()
        if repo is None:
            raise GitCommandError("无法初始化仓库")
        return repo

    # ---------------- 提交 ----------------
    def commit_all(self, message: str, *, tag: str | None = None) -> CommitResult:
        if _git is None:
            return CommitResult(False, error="GitPython 未安装")
        with self._git_lock():
            return self._commit_all(message, tag=tag)

    def _commit_all(self, message: str, tag: str | None = None) -> CommitResult:
        try:
            repo = self._ensure()
            repo.git.add(A=True)
            changed = bool(repo.git.status("--porcelain").strip())
            if not changed:
                return CommitResult(True, sha="", message="noop", tag="")
            commit = repo.index.commit(message)
            sha = commit.hexsha
            # 缺陷 #126：打标失败必须体现在返回值上——tag 置空 + tag_error 写明原因
            tag_err = self._force_tag(repo, tag) if tag else ""
            return CommitResult(
                True, sha=sha, message=message, tag="" if tag_err else (tag or ""), tag_error=tag_err
            )
        except Exception as exc:
            # 缺陷 #99：git 失败不能只是「返回值写着 False」——多数调用方根本不看
            # 返回值（伏笔状态更新、curate 落盘、灵感速记都是拿了就丢），
            # 作者会以为自己改的真相源已经进了版本库，实际没有。至少留下日志。
            logger.warning("git 提交失败：%s -> %s", message, exc)
            return CommitResult(False, error=str(exc))

    def commit_paths(self, paths: list[str], message: str, *, tag: str | None = None) -> CommitResult:
        if _git is None:
            return CommitResult(False, error="GitPython 未安装")
        with self._git_lock():
            return self._commit_paths(paths, message, tag=tag)

    def _commit_paths(self, paths: list[str], message: str, tag: str | None = None) -> CommitResult:
        try:
            repo = self._ensure()
            existing = [p for p in paths if (self.root / p).exists()]
            removed = [p for p in paths if not (self.root / p).exists()]
            if existing:
                repo.git.add("--", *existing)
            if removed:
                for p in removed:
                    try:
                        repo.git.rm("--cached", "--ignore-unmatch", "--", p)
                    except GitCommandError:
                        pass
            if not repo.git.status("--porcelain").strip():
                return CommitResult(True, sha="", message="noop")
            commit = repo.index.commit(message)
            tag_err = self._force_tag(repo, tag) if tag else ""
            return CommitResult(
                True, sha=commit.hexsha, message=message, tag="" if tag_err else (tag or ""), tag_error=tag_err
            )
        except Exception as exc:
            return CommitResult(False, error=str(exc))

    def commit_chapter(self, ch: int, title: str, words: int) -> CommitResult:
        """§4.3：章 final 时自动 commit + tag。"""
        name = f"ch-{ch:04d}"
        msg = f"{name}: {title} ({words:,}字)" if title else f"{name}: ({words:,}字)"
        return self.commit_all(msg, tag=f"ch/{ch:04d}")

    def commit_truth(self, message: str) -> CommitResult:
        return self.commit_all(f"truth: {message}")

    @staticmethod
    def _has_head(repo: Any) -> bool:
        try:
            repo.head.commit  # noqa: B018
            return True
        except Exception:
            return False

    @staticmethod
    def _force_tag(repo: Any, tag: str) -> str:
        """打 tag；返回错误文本（成功返回空串）。

        缺陷 #126：原先 ``except GitCommandError: pass`` —— 打标失败（实测：
        ``.git/refs/tags/ch/0002.lock`` 残留 / 只读挂载 / 磁盘满）时 ``_commit_all``
        仍然 ``return CommitResult(..., tag=tag)``：作者拿到 ok=True 与 ch/0002，
        点时光机却查不到这一章，而且一行日志都没有。
        """
        try:
            repo.git.tag("-f", tag)
            return ""
        except Exception as exc:
            logger.warning("git 打标失败：%s -> %s", tag, exc)
            return str(exc)

    # ---------------- 时光机 ----------------
    def log(self, limit: int = 50) -> list[dict[str, Any]]:
        if _git is None:
            return []
        repo = self._repo()
        if repo is None:
            return []
        try:
            out: list[dict[str, Any]] = []
            for c in repo.iter_commits(max_count=limit):
                out.append(
                    {
                        "sha": c.hexsha,
                        "short": c.hexsha[:8],
                        "message": c.message.strip(),
                        "date": c.committed_datetime.isoformat(timespec="seconds"),
                        "author": c.author.name,
                    }
                )
            return out
        except Exception:
            return []

    def tags(self) -> list[str]:
        repo = self._repo()
        if repo is None:
            return []
        try:
            return sorted(t.name for t in repo.tags)
        except Exception:
            return []

    def show_file_at(self, rev: str, relpath: str) -> str | None:
        """读取某次提交/标签下某文件的内容（版本回溯）。"""
        repo = self._repo()
        if repo is None:
            return None
        try:
            return repo.git.show(f"{rev}:{relpath}")
        except Exception:
            return None

    def diff_between(self, rev_a: str, rev_b: str, relpath: str | None = None) -> str:
        repo = self._repo()
        if repo is None:
            return ""
        try:
            args = [f"{rev_a}..{rev_b}"]
            if relpath:
                args += ["--", relpath]
            return repo.git.diff(*args)
        except Exception:
            return ""

    def file_history(self, relpath: str, limit: int = 30) -> list[dict[str, Any]]:
        repo = self._repo()
        if repo is None:
            return []
        try:
            out = repo.git.log(f"--max-count={limit}", "--pretty=format:%H|%h|%cI|%s", "--", relpath)
        except Exception:
            return []
        rows: list[dict[str, Any]] = []
        for line in out.splitlines():
            parts = line.split("|", 3)
            if len(parts) == 4:
                rows.append({"sha": parts[0], "short": parts[1], "date": parts[2], "message": parts[3]})
        return rows
