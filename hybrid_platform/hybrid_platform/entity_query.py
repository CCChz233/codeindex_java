"""在 SQLite 符号表之上提供实体级查询，不直接暴露原始 SQL。

示例::

    from hybrid_platform.entity_query import find_entity
    from hybrid_platform.storage import SqliteStore

    store = SqliteStore("examples/netty.db")
    rows = find_entity(store, type="class", name="AbstractByteBuf")
    store.close()
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, List, Literal, Optional, Sequence, Tuple

if TYPE_CHECKING:
    from .storage import SqliteStore

# 用户侧 type -> symbols.kind 取值（SCIP/Java 常见）
_ENTITY_TYPE_TO_KINDS: dict[str, Optional[Tuple[str, ...]]] = {
    "class": ("Class", "Record"),
    "interface": ("Interface",),
    "enum": ("Enum",),
    # 「类型」级：类 / 接口 / 枚举
    "type": ("Class", "Interface", "Enum", "Record"),
    "method": ("Method", "StaticMethod", "AbstractMethod"),
    "field": ("Field", "StaticField"),
    "constructor": ("Constructor",),
    "variable": ("Variable",),
    "type_parameter": ("TypeParameter",),
    # 不限制 kind
    "any": None,
}

MatchMode = Literal["exact", "contains"]


def normalize_entity_type(entity_type: str) -> str:
    t = (entity_type or "").strip().lower()
    if t not in _ENTITY_TYPE_TO_KINDS:
        allowed = ", ".join(sorted(_ENTITY_TYPE_TO_KINDS.keys()))
        raise ValueError(f"Unknown entity_type={entity_type!r}; allowed: {allowed}")
    return t


@dataclass(frozen=True)
class EntityHit:
    """单条实体查询结果（便于 JSON 序列化）。"""

    symbol_id: str
    display_name: str
    kind: str
    package: str
    language: str
    enclosing_symbol: str


def find_entity(
    store: SqliteStore,
    *,
    type: str,
    name: str,
    match: MatchMode = "contains",
    package_contains: str = "",
    limit: int = 50,
) -> List[EntityHit]:
    """按实体类型 + 名称查找符号（基于 `symbols` 表）。

    :param type: 逻辑类型，如 ``class``、``method``、``type``（类/接口/枚举）、``any`` 等。
    :param name: 标识名，通常对应 ``display_name`` 或与 ``symbol_id`` 路径片段匹配。
    :param match: ``exact`` 仅等于（忽略大小写）；``contains`` 子串匹配 ``display_name`` 或 ``symbol_id``。
    :param package_contains: 可选，要求 ``package`` 或 ``symbol_id`` 中含该子串（忽略大小写）。
    :param limit: 最大返回条数。
    """
    store.require_capability("find_entity")
    et = normalize_entity_type(type)
    kinds = _ENTITY_TYPE_TO_KINDS[et]

    name = (name or "").strip()
    if not name:
        return []

    pc = (package_contains or "").strip().lower()
    nl = name.lower()
    if match == "exact":
        rows = store.conn.execute(
            """
            SELECT symbol_id, display_name, kind, package, language, enclosing_symbol
            FROM symbols
            WHERE (lower(display_name) = ? OR lower(symbol_id) = ?)
            ORDER BY length(display_name), symbol_id
            LIMIT ?
            """,
            (nl, nl, max(limit, 50)),
        ).fetchall()
    else:
        rows = store._symbol_search_candidates(name, max(limit, 100))

    out: List[EntityHit] = []
    seen: set[str] = set()
    for r in rows:
        kind = str(r["kind"])
        if kinds is not None and kind not in kinds:
            continue
        package = str(r["package"] or "")
        sid = str(r["symbol_id"])
        if pc and pc not in package.lower() and pc not in sid.lower():
            continue
        if sid in seen:
            continue
        seen.add(sid)
        out.append(
            EntityHit(
                symbol_id=sid,
                display_name=str(r["display_name"]),
                kind=kind,
                package=package,
                language=r["language"] or "",
                enclosing_symbol=r["enclosing_symbol"] or "",
            )
        )
        if len(out) >= limit:
            break
    return out


def entity_types() -> Sequence[str]:
    """返回支持的逻辑 ``type`` 名称列表。"""
    return tuple(sorted(_ENTITY_TYPE_TO_KINDS.keys()))
