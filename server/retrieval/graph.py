"""GraphRAG 实体图谱（§8.4）。

数据来源（**全部可从真相源重建，图谱本身不入真相源**）
-------------------------------------------------------
- 静态：``truth/characters/*.yaml``（角色卡 + ``relations``）
- 静态：``truth/world/*.md``（世界观条目，kind="world"）
- 动态：``memory/entity-deltas.jsonl``（**这是真相文件**，``EntityDelta`` 流）

读取的索引表：``entities(entity_id, kind, ch, payload)`` / ``edges(src, rel, dst, ch, weight)``。

边类型中英映射（文档 §8.4）：师徒 / 敌对 / 所属 / 持有 / 亲属。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from server.core.store import NovelStore

from .index import IndexBuilder, _bare_id

#: 关系类型中英映射（中文为规范名，英文/别名都映射回中文）
REL_CANON: dict[str, str] = {
    "师徒": "师徒",
    "master": "师徒",
    "master_apprentice": "师徒",
    "mentor": "师徒",
    "teacher": "师徒",
    "disciple": "师徒",
    "敌对": "敌对",
    "enemy": "敌对",
    "rival": "敌对",
    "hostile": "敌对",
    "所属": "所属",
    "member_of": "所属",
    "belongs_to": "所属",
    "affiliation": "所属",
    "faction": "所属",
    "持有": "持有",
    "owns": "持有",
    "possess": "持有",
    "hold": "持有",
    "亲属": "亲属",
    "kin": "亲属",
    "family": "亲属",
    "relative": "亲属",
    "sibling": "亲属",
    "parent": "亲属",
    "child": "亲属",
}

#: 规范边类型的稳定顺序（渲染时用）
REL_ORDER = ["师徒", "亲属", "敌对", "所属", "持有"]

#: 反向边前缀（IndexBuilder 补的对称边）
_INV_PREFIX = "inv:"

#: 别名边
_ALIAS_REL = "alias_of"


def canon_rel(rel: str) -> str:
    """把任意关系写法归一到中文规范名。未知类型原样返回。"""
    raw = str(rel or "").strip()
    if not raw:
        return ""
    if raw.startswith(_INV_PREFIX):
        inner = canon_rel(raw[len(_INV_PREFIX):])
        return inner or raw
    return REL_CANON.get(raw, REL_CANON.get(raw.lower(), raw))


def is_alias_edge(rel: str) -> bool:
    return str(rel or "").strip() in (_ALIAS_REL, _INV_PREFIX + _ALIAS_REL)


@dataclass
class Entity:
    entity_id: str
    kind: str = ""
    ch: int = 0
    name: str = ""
    payload: dict[str, Any] = field(default_factory=dict)

    @property
    def char_id(self) -> str:
        """``chars/mc`` → ``mc``。"""
        return _bare_id(self.entity_id)

    def excerpt(self, limit: int = 200) -> str:
        """实体卡节选（给 L4 注入用）。"""
        p = self.payload or {}
        parts: list[str] = []
        name = p.get("name") or self.name or self.char_id
        role = p.get("role")
        if role:
            parts.append(f"{name}（{role}）")
        else:
            parts.append(str(name))
        for key, label in (
            ("personality", "性格"),
            ("appearance", "外貌"),
            ("speech_habits", "口癖"),
            ("boundaries", "红线"),
        ):
            val = p.get(key)
            if isinstance(val, list) and val:
                parts.append(f"{label}:" + "、".join(str(v) for v in val))
            elif isinstance(val, str) and val.strip():
                parts.append(f"{label}:{val.strip()}")
        ps = p.get("power_scale")
        if isinstance(ps, dict):
            rank = ps.get("rank") or ""
            ceiling = ps.get("ceiling") or ""
            if rank or ceiling:
                parts.append(f"境界:{rank}/{ceiling}")
        note = p.get("note") or p.get("excerpt")
        if note:
            parts.append(str(note))
        text = "；".join(x for x in parts if x)
        return text[:limit] if limit > 0 else text


@dataclass
class Edge:
    src: str
    rel: str
    dst: str
    ch: int = 0
    weight: float = 1.0

    @property
    def rel_label(self) -> str:
        return canon_rel(self.rel)


@dataclass
class ExpandedEntity:
    """一跳扩展出来的一条实体卡节选。"""

    entity_id: str
    name: str = ""
    kind: str = ""
    rel: str = ""
    via: str = ""
    ch: int = 0
    text: str = ""
    weight: float = 1.0

    def render(self) -> str:
        tag = f"（{self.rel_label}·经 {self.via}）" if self.rel_label or self.via else ""
        return f"- {self.name or self.entity_id}{tag}: {self.text}".rstrip()

    @property
    def rel_label(self) -> str:
        return canon_rel(self.rel)


class GraphIndex:
    """只读图谱视图（在 ``entities``/``edges`` 表之上）。"""

    def __init__(self, store: NovelStore, index: IndexBuilder | None = None):
        self.store = store
        self.index = index or IndexBuilder(store)

    # ---------------------------------------------------------- 底层读取

    def _conn(self):
        return self.index.connect(create=False)

    def get_entity(self, entity_id: str) -> Entity | None:
        """按 ``mc`` / ``chars/mc`` / ``@mc`` 取实体。失败返回 ``None``。"""
        eid = self._eid(entity_id)
        if not eid:
            return None
        try:
            conn = self._conn()
        except Exception:
            return None
        try:
            row = conn.execute(
                "SELECT entity_id, kind, ch, payload FROM entities WHERE entity_id=?", (eid,)
            ).fetchone()
            if row is None:
                # 别名兜底：走 alias_of 边
                alt = conn.execute(
                    "SELECT src FROM edges WHERE dst=? AND rel=?", (eid, _ALIAS_REL)
                ).fetchone()
                if alt is None:
                    return None
                row = conn.execute(
                    "SELECT entity_id, kind, ch, payload FROM entities WHERE entity_id=?",
                    (str(alt[0]),),
                ).fetchone()
                if row is None:
                    return None
            return _row_to_entity(row)
        except Exception:
            return None
        finally:
            conn.close()

    def has_entity(self, entity_id: str) -> bool:
        return self.get_entity(entity_id) is not None

    def all_entities(self, kind: str | None = None) -> list[Entity]:
        try:
            conn = self._conn()
        except Exception:
            return []
        try:
            if kind:
                rows = conn.execute(
                    "SELECT entity_id, kind, ch, payload FROM entities WHERE kind=? ORDER BY entity_id",
                    (kind,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT entity_id, kind, ch, payload FROM entities ORDER BY entity_id"
                ).fetchall()
            return [_row_to_entity(r) for r in rows]
        except Exception:
            return []
        finally:
            conn.close()

    def entity_count(self) -> int:
        try:
            conn = self._conn()
        except Exception:
            return 0
        try:
            row = conn.execute("SELECT COUNT(*) FROM entities").fetchone()
            return int(row[0]) if row else 0
        except Exception:
            return 0
        finally:
            conn.close()

    # ---------------------------------------------------------- 邻居

    def neighbors(
        self,
        entity_id: str,
        hops: int = 1,
        rels: list[str] | None = None,
    ) -> list[Edge]:
        """BFS 取邻居边。``rels`` 按**中文规范名或英文别名**过滤。

        返回的 ``Edge.src`` 始终是被查询实体，``Edge.dst`` 是邻居；
        反向边（``inv:``）会被归一成正向语义但不改方向，只把 ``rel`` 规范化。
        """
        start = self._eid(entity_id)
        if not start:
            return []
        try:
            hop_n = max(1, int(hops))
        except (TypeError, ValueError):
            hop_n = 1
        allow = {canon_rel(r) for r in (rels or []) if str(r or "").strip()} or None

        seen_nodes: set[str] = {start}
        out: list[Edge] = []
        seen_edges: set[tuple[str, str, str]] = set()
        frontier: list[str] = [start]
        try:
            conn = self._conn()
        except Exception:
            return []
        try:
            for _ in range(hop_n):
                if not frontier:
                    break
                nxt: list[str] = []
                for node in frontier:
                    rows = conn.execute(
                        "SELECT src, rel, dst, ch, weight FROM edges WHERE src=? OR dst=?",
                        (node, node),
                    ).fetchall()
                    for r in rows:
                        src = str(r["src"])
                        dst = str(r["dst"])
                        rel = str(r["rel"])
                        other = dst if src == node else src
                        if other == node or is_alias_edge(rel):
                            continue
                        label = canon_rel(rel)
                        if allow is not None and label not in allow:
                            continue
                        key = (node, label, other)
                        if key in seen_edges:
                            continue
                        seen_edges.add(key)
                        out.append(
                            Edge(
                                src=node,
                                rel=label,
                                dst=other,
                                ch=int(r["ch"] or 0),
                                weight=float(r["weight"] or 1.0),
                            )
                        )
                        if other not in seen_nodes:
                            seen_nodes.add(other)
                            nxt.append(other)
                frontier = nxt
            return out
        except Exception:
            return out
        finally:
            conn.close()

    # ---------------------------------------------------------- 一跳扩展

    def expand_context(
        self,
        char_ids: list[str],
        budget_chars: int = 1200,
    ) -> list[ExpandedEntity]:
        """L1 出场人物 → 一跳关系网 → 相关实体卡节选（总长受 ``budget_chars`` 约束）。

        排序策略：直连边权重高者优先，其次按边类型稳定序（师徒/亲属/敌对/所属/持有），
        再次按章号。已在 ``char_ids`` 里的角色不再重复返回。
        """
        seeds = [_bare_id(c) for c in (char_ids or []) if str(c or "").strip()]
        seeds = list(dict.fromkeys(s for s in seeds if s))
        if not seeds:
            return []
        try:
            budget = max(0, int(budget_chars))
        except (TypeError, ValueError):
            budget = 1200

        seed_entities = {s: self.get_entity(s) for s in seeds}
        results: list[ExpandedEntity] = []
        used = 0
        seen: set[str] = set(seeds)

        candidates: list[tuple[float, int, int, Edge]] = []
        rel_rank = {r: i for i, r in enumerate(REL_ORDER)}
        for seed in seeds:
            for e in self.neighbors(seed, hops=1):
                target = _bare_id(e.dst)
                if not target or target in seen:
                    continue
                candidates.append(
                    (e.weight, rel_rank.get(e.rel_label, len(REL_ORDER)), e.ch, e)
                )
        candidates.sort(key=lambda t: (-t[0], t[1], t[2], t[3].dst))

        for weight, _rr, _ch, edge in candidates:
            target = _bare_id(edge.dst)
            if target in seen:
                continue
            ent = self.get_entity(target)
            if ent is None:
                continue
            seen.add(target)
            text = ent.excerpt(limit=max(0, budget - used))
            if not text:
                continue
            if used + len(text) > budget and results:
                break
            item = ExpandedEntity(
                entity_id=target,
                name=ent.name or ent.char_id,
                kind=ent.kind or "character",
                rel=edge.rel_label or edge.rel,
                via=_bare_id(edge.src),
                ch=edge.ch or ent.ch,
                text=text,
                weight=weight,
            )
            results.append(item)
            used += len(text)
            if used >= budget:
                break
        return results

    # ---------------------------------------------------------- 渲染

    def render_expanded(self, expanded: Sequence[ExpandedEntity], budget_chars: int = 1200) -> str:
        """把手动拼好的 ``ExpandedEntity`` 列表渲染成可注入文本（受预算约束）。"""
        lines: list[str] = []
        used = 0
        for item in expanded:
            line = item.render()
            if used + len(line) > budget_chars:
                break
            lines.append(line)
            used += len(line) + 1
        return "\n".join(lines)

    # ---------------------------------------------------------- 工具

    @staticmethod
    def _eid(entity_id: str) -> str:
        raw = str(entity_id or "").strip()
        if not raw:
            return ""
        if raw.startswith(("chars/", "world/")):
            return raw
        bare = _bare_id(raw)
        return f"chars/{bare}" if bare else ""


def _row_to_entity(row: Any) -> Entity:
    import json

    eid = str(row["entity_id"])
    payload_raw = row["payload"] or "{}"
    try:
        payload = json.loads(payload_raw) if isinstance(payload_raw, str) else dict(payload_raw)
    except Exception:
        payload = {}
    name = str(payload.get("name") or _bare_id(eid))
    return Entity(
        entity_id=eid,
        kind=str(row["kind"] or ""),
        ch=int(row["ch"] or 0),
        name=name,
        payload=payload,
    )


__all__ = [
    "REL_CANON",
    "REL_ORDER",
    "canon_rel",
    "is_alias_edge",
    "Entity",
    "Edge",
    "ExpandedEntity",
    "GraphIndex",
]
