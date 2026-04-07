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
    "class": ("Class",),
    "interface": ("Interface",),
    "enum": ("Enum",),
    # 「类型」级：类 / 接口 / 枚举
    "type": ("Class", "Interface", "Enum"),
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
    et = normalize_entity_type(type)
    kinds = _ENTITY_TYPE_TO_KINDS[et]

    name = (name or "").strip()
    if not name:
        return []

    pc = (package_contains or "").strip().lower()
    params: List[object] = []
    where_kind = ""
    if kinds is not None:
        placeholders = ",".join("?" * len(kinds))
        where_kind = f" AND kind IN ({placeholders})"
        params.extend(kinds)

    nl = name.lower()

    if match == "exact":
        where_name = " AND (lower(display_name) = ? OR lower(symbol_id) = ?)"
        params.extend([nl, nl])
    else:
        qn = f"%{nl}%"
        where_name = " AND (lower(display_name) LIKE ? OR lower(symbol_id) LIKE ?)"
        params.extend([qn, qn])

    where_pkg = ""
    if pc:
        where_pkg = " AND (lower(package) LIKE ? OR lower(symbol_id) LIKE ?)"
        pcp = f"%{pc}%"
        params.extend([pcp, pcp])

    # ORDER BY：完全匹配 display_name 优先，再按名称长度、symbol_id
    params.append(nl)
    params.append(limit)

    sql = f"""
        SELECT symbol_id, display_name, kind, package, language, enclosing_symbol
        FROM symbols
        WHERE 1=1
        {where_kind}
        {where_name}
        {where_pkg}
        ORDER BY
          CASE WHEN lower(display_name) = ? THEN 0 ELSE 1 END,
          length(display_name),
          symbol_id
        LIMIT ?
    """
    rows = store.conn.execute(sql, params).fetchall()

    out: List[EntityHit] = []
    for r in rows:
        out.append(
            EntityHit(
                symbol_id=r["symbol_id"],
                display_name=r["display_name"],
                kind=r["kind"],
                package=r["package"],
                language=r["language"] or "",
                enclosing_symbol=r["enclosing_symbol"] or "",
            )
        )
    return out


def entity_types() -> Sequence[str]:
    """返回支持的逻辑 ``type`` 名称列表。"""
    return tuple(sorted(_ENTITY_TYPE_TO_KINDS.keys()))
