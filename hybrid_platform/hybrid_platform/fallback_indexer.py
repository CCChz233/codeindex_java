from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Sequence

from .models import OccurrenceEdge, RelationEdge, ScipDocument, SymbolNode
from .storage import SqliteStore


@dataclass(frozen=True)
class FallbackBuildStats:
    source_mode: str
    documents: int
    symbols: int
    occurrences: int
    relations: int

    def as_dict(self) -> dict[str, object]:
        return {
            "source_mode": self.source_mode,
            "documents": self.documents,
            "symbols": self.symbols,
            "occurrences": self.occurrences,
            "relations": self.relations,
        }


def _fingerprint(symbol_id: str, display_name: str, kind: str) -> str:
    payload = f"{symbol_id}|{display_name}|{kind}".encode("utf-8")
    return hashlib.sha1(payload).hexdigest()


def _sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _iter_java_files(repo_root: str) -> Iterator[Path]:
    root = Path(repo_root)
    for path in sorted(root.rglob("*.java")):
        if path.is_file():
            yield path


def _get_java_parser() -> object | None:
    try:
        from tree_sitter import Language, Parser  # type: ignore
        import tree_sitter_java  # type: ignore
    except Exception:
        return None
    parser = Parser()
    parser.language = Language(tree_sitter_java.language())
    return parser


def _walk(node: object) -> Iterator[object]:
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        children = getattr(current, "children", []) or []
        for child in reversed(children):
            stack.append(child)


def _node_text(node: object, source_bytes: bytes) -> str:
    return source_bytes[int(getattr(node, "start_byte")) : int(getattr(node, "end_byte"))].decode(
        "utf-8", errors="replace"
    )


def _named_children(node: object) -> list[object]:
    return [child for child in (getattr(node, "children", []) or []) if getattr(child, "is_named", False)]


def _find_name_node(node: object) -> object | None:
    child_by_field_name = getattr(node, "child_by_field_name", None)
    if callable(child_by_field_name):
        named = child_by_field_name("name")
        if named is not None:
            return named
    for child in _named_children(node):
        if str(getattr(child, "type", "")) in {"identifier", "type_identifier"}:
            return child
        if str(getattr(child, "type", "")) == "variable_declarator":
            found = _find_name_node(child)
            if found is not None:
                return found
    return None


def _range_from_node(node: object) -> tuple[int, int, int, int]:
    start = getattr(node, "start_point")
    end = getattr(node, "end_point")
    return int(start.row), int(start.column), int(end.row), int(end.column)


def _count_parameters(node: object) -> int:
    child_by_field_name = getattr(node, "child_by_field_name", None)
    params = child_by_field_name("parameters") if callable(child_by_field_name) else None
    if params is None:
        return 0
    count = 0
    for child in _named_children(params):
        ctype = str(getattr(child, "type", ""))
        if ctype.endswith("parameter"):
            count += 1
    return count


def _strip_generics(text: str) -> str:
    out: list[str] = []
    depth = 0
    for ch in text:
        if ch == "<":
            depth += 1
            continue
        if ch == ">":
            depth = max(0, depth - 1)
            continue
        if depth == 0:
            out.append(ch)
    return "".join(out)


def _split_type_refs(text: str) -> list[str]:
    cleaned = _strip_generics(text or "")
    parts = [part.strip() for part in cleaned.split(",") if part.strip()]
    out: list[str] = []
    for item in parts:
        tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_$.]*", item)
        if tokens:
            out.append(tokens[-1])
    return out


def _node_children_by_type(node: object, target_type: str) -> list[object]:
    return [child for child in _named_children(node) if str(getattr(child, "type", "")) == target_type]


def _child_by_type(node: object, target_type: str) -> object | None:
    for child in _named_children(node):
        if str(getattr(child, "type", "")) == target_type:
            return child
    return None


@dataclass(frozen=True)
class _TypeSymbol:
    symbol_id: str
    qualified_name: str
    simple_name: str
    package: str


@dataclass(frozen=True)
class _PendingRelation:
    from_symbol: str
    relation_type: str
    target_name: str
    relative_path: str
    package: str


class SyntaxFallbackIndexer:
    def __init__(self, store: SqliteStore) -> None:
        self.store = store

    def run(self, repo_root: str, repo: str, commit: str) -> FallbackBuildStats:
        parser = _get_java_parser()
        if parser is None:
            raise RuntimeError("syntax fallback requires tree-sitter and tree-sitter-java")

        documents: list[ScipDocument] = []
        symbols: list[SymbolNode] = []
        occurrences: list[OccurrenceEdge] = []
        relations: list[RelationEdge] = []
        pending: list[_PendingRelation] = []
        known_types: list[_TypeSymbol] = []

        root = Path(repo_root).resolve()
        for path in _iter_java_files(repo_root):
            relative_path = str(path.resolve().relative_to(root))
            content = path.read_text(encoding="utf-8", errors="replace")
            source_bytes = content.encode("utf-8")
            tree = parser.parse(source_bytes)
            package_name = ""
            package_node = _child_by_type(tree.root_node, "package_declaration")
            if package_node is not None:
                package_name = _node_text(package_node, source_bytes)
                package_name = package_name.replace("package", "").replace(";", "").strip()

            doc_symbols: list[SymbolNode] = []
            doc_occurrences: list[OccurrenceEdge] = []
            doc_pending: list[_PendingRelation] = []
            doc_types: list[_TypeSymbol] = []

            def visit(node: object, enclosing: Sequence[_TypeSymbol]) -> None:
                node_type = str(getattr(node, "type", ""))
                owner = enclosing[-1] if enclosing else None
                if node_type in {
                    "class_declaration",
                    "interface_declaration",
                    "enum_declaration",
                    "record_declaration",
                    "annotation_type_declaration",
                }:
                    name_node = _find_name_node(node)
                    if name_node is None:
                        return
                    display_name = _node_text(name_node, source_bytes).strip()
                    if not display_name:
                        return
                    kind_token = {
                        "class_declaration": ("Class", "class"),
                        "interface_declaration": ("Interface", "interface"),
                        "enum_declaration": ("Enum", "enum"),
                        "record_declaration": ("Record", "record"),
                        "annotation_type_declaration": ("Interface", "interface"),
                    }[node_type]
                    qname_parts = [part for part in [package_name, *(x.simple_name for x in enclosing), display_name] if part]
                    qualified_name = ".".join(qname_parts)
                    symbol_id = f"ts:{relative_path}#{qualified_name}:{kind_token[1]}"
                    symbol = SymbolNode(
                        symbol_id=symbol_id,
                        display_name=display_name,
                        kind=kind_token[0],
                        package=package_name,
                        signature_hash=_sha1(symbol_id),
                        symbol_fingerprint=_fingerprint(symbol_id, display_name, kind_token[0]),
                        enclosing_symbol=owner.symbol_id if owner is not None else "",
                        language="java",
                    )
                    doc_symbols.append(symbol)
                    doc_types.append(
                        _TypeSymbol(
                            symbol_id=symbol_id,
                            qualified_name=qualified_name,
                            simple_name=display_name,
                            package=package_name,
                        )
                    )
                    sl, sc, el, ec = _range_from_node(name_node)
                    doc_occurrences.append(
                        OccurrenceEdge(
                            document_id=f"{repo}:{commit}:{relative_path}",
                            symbol_id=symbol_id,
                            range_start_line=sl,
                            range_start_col=sc,
                            range_end_line=el,
                            range_end_col=ec,
                            role="definition",
                            syntax_kind=node_type,
                        )
                    )
                    if owner is not None:
                        doc_pending.append(
                            _PendingRelation(
                                from_symbol=symbol_id,
                                relation_type="belongs_to",
                                target_name=owner.qualified_name,
                                relative_path=relative_path,
                                package=package_name,
                            )
                        )
                    child_by_field_name = getattr(node, "child_by_field_name", None)
                    super_node = child_by_field_name("superclass") if callable(child_by_field_name) else None
                    if super_node is None:
                        super_node = _child_by_type(node, "superclass")
                    if super_node is not None:
                        for target in _split_type_refs(_node_text(super_node, source_bytes)):
                            doc_pending.append(
                                _PendingRelation(
                                    from_symbol=symbol_id,
                                    relation_type="extends",
                                    target_name=target,
                                    relative_path=relative_path,
                                    package=package_name,
                                )
                            )
                    for node_name, relation_type in (
                        ("super_interfaces", "implements"),
                        ("extends_interfaces", "extends"),
                    ):
                        intf_node = _child_by_type(node, node_name)
                        if intf_node is None:
                            continue
                        for target in _split_type_refs(_node_text(intf_node, source_bytes)):
                            doc_pending.append(
                                _PendingRelation(
                                    from_symbol=symbol_id,
                                    relation_type=relation_type,
                                    target_name=target,
                                    relative_path=relative_path,
                                    package=package_name,
                                )
                            )
                    body = child_by_field_name("body") if callable(child_by_field_name) else None
                    for child in _named_children(body or node):
                        if child is body:
                            continue
                        visit(child, [*enclosing, doc_types[-1]])
                    return

                if owner is None:
                    return

                if node_type == "constructor_declaration":
                    name_node = _find_name_node(node)
                    if name_node is None:
                        return
                    display_name = owner.simple_name
                    arity = _count_parameters(node)
                    symbol_id = f"ts:{relative_path}#{owner.qualified_name}.{display_name}:constructor:{arity}"
                    kind = "Constructor"
                    doc_symbols.append(
                        SymbolNode(
                            symbol_id=symbol_id,
                            display_name=display_name,
                            kind=kind,
                            package=package_name,
                            signature_hash=_sha1(symbol_id),
                            symbol_fingerprint=_fingerprint(symbol_id, display_name, kind),
                            enclosing_symbol=owner.symbol_id,
                            language="java",
                        )
                    )
                    sl, sc, el, ec = _range_from_node(name_node)
                    doc_occurrences.append(
                        OccurrenceEdge(
                            document_id=f"{repo}:{commit}:{relative_path}",
                            symbol_id=symbol_id,
                            range_start_line=sl,
                            range_start_col=sc,
                            range_end_line=el,
                            range_end_col=ec,
                            role="definition",
                            syntax_kind=node_type,
                        )
                    )
                    doc_pending.append(
                        _PendingRelation(
                            from_symbol=symbol_id,
                            relation_type="belongs_to",
                            target_name=owner.qualified_name,
                            relative_path=relative_path,
                            package=package_name,
                        )
                    )
                    return

                if node_type == "method_declaration":
                    name_node = _find_name_node(node)
                    if name_node is None:
                        return
                    display_name = _node_text(name_node, source_bytes).strip()
                    if not display_name:
                        return
                    arity = _count_parameters(node)
                    symbol_id = f"ts:{relative_path}#{owner.qualified_name}.{display_name}:method:{arity}"
                    kind = "Method"
                    doc_symbols.append(
                        SymbolNode(
                            symbol_id=symbol_id,
                            display_name=display_name,
                            kind=kind,
                            package=package_name,
                            signature_hash=_sha1(symbol_id),
                            symbol_fingerprint=_fingerprint(symbol_id, display_name, kind),
                            enclosing_symbol=owner.symbol_id,
                            language="java",
                        )
                    )
                    sl, sc, el, ec = _range_from_node(name_node)
                    doc_occurrences.append(
                        OccurrenceEdge(
                            document_id=f"{repo}:{commit}:{relative_path}",
                            symbol_id=symbol_id,
                            range_start_line=sl,
                            range_start_col=sc,
                            range_end_line=el,
                            range_end_col=ec,
                            role="definition",
                            syntax_kind=node_type,
                        )
                    )
                    doc_pending.append(
                        _PendingRelation(
                            from_symbol=symbol_id,
                            relation_type="belongs_to",
                            target_name=owner.qualified_name,
                            relative_path=relative_path,
                            package=package_name,
                        )
                    )
                    return

                if node_type == "field_declaration":
                    for decl in _node_children_by_type(node, "variable_declarator"):
                        name_node = _find_name_node(decl)
                        if name_node is None:
                            continue
                        display_name = _node_text(name_node, source_bytes).strip()
                        if not display_name:
                            continue
                        symbol_id = f"ts:{relative_path}#{owner.qualified_name}.{display_name}:field"
                        kind = "Field"
                        doc_symbols.append(
                            SymbolNode(
                                symbol_id=symbol_id,
                                display_name=display_name,
                                kind=kind,
                                package=package_name,
                                signature_hash=_sha1(symbol_id),
                                symbol_fingerprint=_fingerprint(symbol_id, display_name, kind),
                                enclosing_symbol=owner.symbol_id,
                                language="java",
                            )
                        )
                        sl, sc, el, ec = _range_from_node(name_node)
                        doc_occurrences.append(
                            OccurrenceEdge(
                                document_id=f"{repo}:{commit}:{relative_path}",
                                symbol_id=symbol_id,
                                range_start_line=sl,
                                range_start_col=sc,
                                range_end_line=el,
                                range_end_col=ec,
                                role="definition",
                                syntax_kind=node_type,
                            )
                        )
                        doc_pending.append(
                            _PendingRelation(
                                from_symbol=symbol_id,
                                relation_type="belongs_to",
                                target_name=owner.qualified_name,
                                relative_path=relative_path,
                                package=package_name,
                            )
                        )
                    return

            for child in _named_children(tree.root_node):
                visit(child, [])

            documents.append(
                ScipDocument(
                    document_id=f"{repo}:{commit}:{relative_path}",
                    relative_path=relative_path,
                    language="java",
                    occurrence_count=len(doc_occurrences),
                    content=content,
                )
            )
            symbols.extend(doc_symbols)
            occurrences.extend(doc_occurrences)
            pending.extend(doc_pending)
            known_types.extend(doc_types)

        by_qname = {item.qualified_name: item.symbol_id for item in known_types}
        by_package_and_simple = {
            (item.package, item.simple_name): item.symbol_id
            for item in known_types
        }
        by_simple: dict[str, list[str]] = {}
        for item in known_types:
            by_simple.setdefault(item.simple_name, []).append(item.symbol_id)

        def resolve_type(target_name: str, package_name: str) -> str | None:
            raw = (target_name or "").strip()
            if not raw:
                return None
            if raw in by_qname:
                return by_qname[raw]
            short = raw.rsplit(".", 1)[-1]
            if (package_name, short) in by_package_and_simple:
                return by_package_and_simple[(package_name, short)]
            candidates = by_simple.get(short, [])
            if len(candidates) == 1:
                return candidates[0]
            return None

        for item in pending:
            target_symbol = resolve_type(item.target_name, item.package)
            if target_symbol is None:
                continue
            relations.append(
                RelationEdge(
                    from_symbol=item.from_symbol,
                    to_symbol=target_symbol,
                    relation_type=item.relation_type,
                    confidence=1.0,
                    evidence_document_id=f"{repo}:{commit}:{item.relative_path}",
                )
            )

        self.store.clear_index_data()
        if documents:
            self.store.upsert_documents(repo, commit, documents)
        if symbols:
            self.store.upsert_symbols(symbols)
        if occurrences:
            self.store.insert_occurrences(occurrences)
        if relations:
            self.store.insert_relations(relations)
        self.store.commit()
        return FallbackBuildStats(
            source_mode="syntax",
            documents=len(documents),
            symbols=len(symbols),
            occurrences=len(occurrences),
            relations=len(relations),
        )


class DocumentFallbackIndexer:
    def __init__(self, store: SqliteStore) -> None:
        self.store = store

    def run(self, repo_root: str, repo: str, commit: str) -> FallbackBuildStats:
        root = Path(repo_root).resolve()
        documents: list[ScipDocument] = []
        for path in _iter_java_files(repo_root):
            relative_path = str(path.resolve().relative_to(root))
            content = path.read_text(encoding="utf-8", errors="replace")
            documents.append(
                ScipDocument(
                    document_id=f"{repo}:{commit}:{relative_path}",
                    relative_path=relative_path,
                    language="java",
                    occurrence_count=0,
                    content=content,
                )
            )
        self.store.clear_index_data()
        if documents:
            self.store.upsert_documents(repo, commit, documents)
        self.store.commit()
        return FallbackBuildStats(
            source_mode="document",
            documents=len(documents),
            symbols=0,
            occurrences=0,
            relations=0,
        )
