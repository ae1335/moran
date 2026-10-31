"""应用上下文：把各层装配成可用的对象图（依赖注入容器）。

依赖方向单向：api → agents → context/model → core（§3.2），容器在这里完成接线。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..agents.curator import Curator
from ..agents.deconstructor import Deconstructor
from ..agents.naming import NameGenerator
from ..agents.planner import Planner
from ..agents.reviewer import Reviewer
from ..agents.writer import Writer
from ..context.assembler import ContextAssembler
from ..core.store import NovelStore, Workbench
from ..feedback.ingest import FeedbackIngest
from ..model.gateway import ModelGateway
from ..model.usage import UsageDB
from ..platform.cover import CoverGenerator
from ..platform.exporter import Exporter
from ..platform.polish import AIPolish
from ..platform.setting_book import SettingBookExporter
from ..stats.writing import WritingStats


def _make_retrieval(store: NovelStore) -> Any | None:
    """检索层可选依赖：导入失败不影响主流程（降级为无 L4）。"""
    try:
        from ..retrieval.facade import RetrievalFacade  # 延迟导入
    except Exception:
        return None
    try:
        return RetrievalFacade(store)
    except Exception:
        return None


@dataclass
class BookContext:
    """一本书的全套服务。"""

    store: NovelStore
    gateway: ModelGateway
    retrieval: Any | None = None
    _assembler: ContextAssembler | None = None

    @property
    def assembler(self) -> ContextAssembler:
        if self._assembler is None:
            book = None
            book_err = ""
            try:
                book = self.store.load_book()
            except Exception as exc:
                # 缺陷 #130：load_book 挂了，静默用默认 16000，作者改的 context_budget 被悄悄忽略。
                # 必须记 warning，否则作者永远不知道为什么预算没生效。
                book_err = f"book.yaml 读取失败：{type(exc).__name__}，上下文预算已降级为默认 16000"
            budget = book.context_budget if book else 16000
            self._assembler = ContextAssembler(self.store, retrieval=self.retrieval, budget=budget)
            if book_err:
                self._assembler.warnings.append(book_err)
        return self._assembler

    @property
    def planner(self) -> Planner:
        return Planner(self.store, self.gateway)

    @property
    def writer(self) -> Writer:
        return Writer(self.store, self.gateway, self.assembler)

    @property
    def reviewer(self) -> Reviewer:
        return Reviewer(self.store, self.gateway)

    @property
    def curator(self) -> Curator:
        return Curator(self.store, self.gateway)

    @property
    def deconstructor(self) -> Deconstructor:
        return Deconstructor(self.store, self.gateway)

    @property
    def fix_suggester(self):
        from ..platform.fix_suggest import FixSuggester

        return FixSuggester(self.store)

    @property
    def naming(self) -> NameGenerator:
        return NameGenerator(self.store)

    @property
    def exporter(self) -> Exporter:
        # 缺陷 #123：把 polish 传进去，导出预检才会真的扫一遍套语 / 复读。
        # scan() 是纯规则层，不调模型 —— 这里传 gateway 只是为了让 polish 属性自洽。
        return Exporter(self.store, self.polish)

    @property
    def setting_book(self) -> SettingBookExporter:
        return SettingBookExporter(self.store)

    @property
    def cover(self) -> CoverGenerator:
        return CoverGenerator(self.store)

    @property
    def polish(self) -> AIPolish:
        return AIPolish(self.store, self.gateway)

    @property
    def feedback(self) -> FeedbackIngest:
        return FeedbackIngest(self.store)

    @property
    def stats(self) -> WritingStats:
        return WritingStats(self.store)


@dataclass
class AppContext:
    """工作区级服务。"""

    root: Path
    workbench: Workbench
    gateway: ModelGateway
    usage: UsageDB
    _books: dict[str, BookContext] = field(default_factory=dict)

    @classmethod
    def create(cls, root: Path | str | None = None) -> "AppContext":
        r = Path(root or os.environ.get("NOVEL_WORKBENCH_ROOT") or Path.cwd())
        wb = Workbench(r)
        config_dir = wb.paths.config_dir
        usage = UsageDB(wb.paths.db_path)
        gw = ModelGateway(config_dir, usage_db=usage)
        return cls(root=r, workbench=wb, gateway=gw, usage=usage)

    def book(self, dirname: str) -> BookContext:
        if dirname not in self._books:
            store = self.workbench.get_store(dirname)
            self._books[dirname] = BookContext(
                store=store, gateway=self.gateway, retrieval=_make_retrieval(store)
            )
        return self._books[dirname]

    def find_book(self, key: str) -> BookContext:
        store = self.workbench.find_store(key)
        name = store.p.root.name
        if name not in self._books:
            self._books[name] = BookContext(
                store=store, gateway=self.gateway, retrieval=_make_retrieval(store)
            )
        return self._books[name]

    def invalidate(self, dirname: str | None = None) -> None:
        if dirname:
            self._books.pop(dirname, None)
        else:
            self._books.clear()


_ctx: AppContext | None = None


def get_context() -> AppContext:
    global _ctx
    if _ctx is None:
        _ctx = AppContext.create()
    return _ctx


def set_context(ctx: AppContext) -> None:
    global _ctx
    _ctx = ctx
