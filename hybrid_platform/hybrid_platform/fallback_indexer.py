from __future__ import annotations

import hashlib
import re
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import AbstractSet, Iterable, Iterator, Mapping, Sequence

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


def _child_by_field_name(node: object, field_name: str) -> object | None:
    child_by_field_name = getattr(node, "child_by_field_name", None)
    if not callable(child_by_field_name):
        return None
    return child_by_field_name(field_name)


def _same_node(left: object | None, right: object | None) -> bool:
    if left is None or right is None:
        return False
    return (
        int(getattr(left, "start_byte", -1)) == int(getattr(right, "start_byte", -2))
        and int(getattr(left, "end_byte", -1)) == int(getattr(right, "end_byte", -2))
    )


def _is_child_field(parent: object | None, field_name: str, child: object) -> bool:
    if parent is None:
        return False
    return _same_node(_child_by_field_name(parent, field_name), child)


def _extract_java_imports(content: str) -> _ImportContext:
    direct: dict[str, str] = {}
    wildcard_packages: list[str] = []
    pattern = re.compile(r"^\s*import\s+(?:static\s+)?([A-Za-z_][A-Za-z0-9_$.]*)(\.\*)?\s*;", re.MULTILINE)
    for match in pattern.finditer(content):
        qualified = match.group(1).strip()
        is_wildcard = bool(match.group(2))
        if is_wildcard:
            wildcard_packages.append(qualified)
            continue
        short = qualified.rsplit(".", 1)[-1]
        direct[short] = qualified
    return _ImportContext(direct=direct, wildcard_packages=tuple(wildcard_packages))


def _argument_count(node: object) -> int:
    args = _child_by_field_name(node, "arguments") or _child_by_type(node, "argument_list")
    if args is None:
        return 0
    return len(_named_children(args))


def _type_text_from_node(node: object | None, source_bytes: bytes) -> str:
    if node is None:
        return ""
    return _node_text(node, source_bytes).strip()


def _first_type_child(node: object) -> object | None:
    typed = _child_by_field_name(node, "type")
    if typed is not None:
        return typed
    for child in _named_children(node):
        ctype = str(getattr(child, "type", ""))
        if "type" in ctype or ctype in {"identifier", "scoped_identifier"}:
            return child
    return None


_ANNOTATION_NODE_TYPES = {
    "annotation",
    "marker_annotation",
    "single_element_annotation",
}


def _annotation_name_node(annotation_node: object) -> object | None:
    named = _child_by_field_name(annotation_node, "name")
    if named is not None:
        return named
    for child in _named_children(annotation_node):
        ctype = str(getattr(child, "type", ""))
        if ctype in {"identifier", "type_identifier", "scoped_identifier", "scoped_type_identifier"}:
            return child
    return None


def _annotation_name_text(name: str) -> str:
    raw = (name or "").strip()
    if raw.startswith("@"):
        raw = raw[1:]
    raw = raw.split("(", 1)[0].strip()
    return raw


def _iter_declaration_annotation_name_nodes(node: object) -> Iterator[object]:
    for child in _named_children(node):
        ctype = str(getattr(child, "type", ""))
        if ctype == "modifiers":
            for modifier in _named_children(child):
                if str(getattr(modifier, "type", "")) in _ANNOTATION_NODE_TYPES:
                    name_node = _annotation_name_node(modifier)
                    if name_node is not None:
                        yield name_node
        elif ctype in _ANNOTATION_NODE_TYPES:
            name_node = _annotation_name_node(child)
            if name_node is not None:
                yield name_node


def _iter_parameter_annotation_name_nodes(node: object) -> Iterator[object]:
    params = _child_by_field_name(node, "parameters")
    if params is None:
        return
    for child in _walk(params):
        ctype = str(getattr(child, "type", ""))
        if ctype in {"formal_parameter", "spread_parameter", "catch_formal_parameter", "receiver_parameter"}:
            yield from _iter_declaration_annotation_name_nodes(child)


def _best_effort_annotation_qname(
    target_name: str,
    package_name: str,
    imports: "_ImportContext | None",
) -> str:
    raw = _annotation_name_text(target_name)
    if not raw:
        return ""
    short = raw.rsplit(".", 1)[-1]
    if imports is not None:
        imported = imports.direct.get(short)
        if imported:
            return imported
    if "." in raw:
        return raw
    if imports is not None and len(imports.wildcard_packages) == 1:
        return f"{imports.wildcard_packages[0]}.{short}"
    return f"{package_name}.{short}" if package_name else short


@dataclass(frozen=True)
class _TypeSymbol:
    symbol_id: str
    qualified_name: str
    simple_name: str
    package: str
    kind: str


@dataclass(frozen=True)
class _MethodSymbol:
    symbol_id: str
    owner_qualified_name: str
    simple_name: str
    arity: int
    package: str
    relative_path: str
    is_constructor: bool = False


@dataclass(frozen=True)
class _FieldSymbol:
    symbol_id: str
    owner_qualified_name: str
    simple_name: str
    package: str
    relative_path: str
    declared_type: str = ""


@dataclass(frozen=True)
class _ImportContext:
    direct: Mapping[str, str]
    wildcard_packages: tuple[str, ...]


@dataclass(frozen=True)
class _PendingRelation:
    from_symbol: str
    relation_type: str
    target_name: str
    relative_path: str
    package: str
    imports: _ImportContext
    range_start_line: int = -1
    range_start_col: int = -1
    range_end_line: int = -1
    range_end_col: int = -1
    syntax_kind: str = ""


@dataclass(frozen=True)
class _PendingAnnotation:
    from_symbol: str
    target_name: str
    relative_path: str
    package: str
    imports: _ImportContext
    range_start_line: int
    range_start_col: int
    range_end_line: int
    range_end_col: int
    enclosing_range_start_line: int
    enclosing_range_start_col: int
    enclosing_range_end_line: int
    enclosing_range_end_col: int
    syntax_kind: str


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
        pending_annotations: list[_PendingAnnotation] = []
        known_types: list[_TypeSymbol] = []
        known_methods: list[_MethodSymbol] = []
        known_fields: list[_FieldSymbol] = []
        doc_imports: dict[str, _ImportContext] = {}

        root = Path(repo_root).resolve()
        for path in _iter_java_files(repo_root):
            relative_path = str(path.resolve().relative_to(root))
            content = path.read_text(encoding="utf-8", errors="replace")
            source_bytes = content.encode("utf-8")
            tree = parser.parse(source_bytes)
            imports = _extract_java_imports(content)
            doc_imports[relative_path] = imports
            package_name = ""
            package_node = _child_by_type(tree.root_node, "package_declaration")
            if package_node is not None:
                package_name = _node_text(package_node, source_bytes)
                package_name = package_name.replace("package", "").replace(";", "").strip()

            doc_symbols: list[SymbolNode] = []
            doc_occurrences: list[OccurrenceEdge] = []
            doc_pending: list[_PendingRelation] = []
            doc_pending_annotations: list[_PendingAnnotation] = []
            doc_types: list[_TypeSymbol] = []
            document_id = f"{repo}:{commit}:{relative_path}"

            if package_name and package_node is not None:
                package_symbol_id = f"pkg:{package_name}"
                package_name_node = _child_by_type(package_node, "scoped_identifier") or _find_name_node(package_node)
                package_name_node = package_name_node or package_node
                kind = "Package"
                doc_symbols.append(
                    SymbolNode(
                        symbol_id=package_symbol_id,
                        display_name=package_name,
                        kind=kind,
                        package=package_name,
                        signature_hash=_sha1(package_symbol_id),
                        symbol_fingerprint=_fingerprint(package_symbol_id, package_name, kind),
                        language="java",
                    )
                )
                sl, sc, el, ec = _range_from_node(package_name_node)
                doc_occurrences.append(
                    OccurrenceEdge(
                        document_id=document_id,
                        symbol_id=package_symbol_id,
                        range_start_line=sl,
                        range_start_col=sc,
                        range_end_line=el,
                        range_end_col=ec,
                        role="definition",
                        syntax_kind="package_declaration",
                    )
                )

            for import_node in _node_children_by_type(tree.root_node, "import_declaration"):
                import_text = _node_text(import_node, source_bytes).strip()
                import_target = re.sub(r"^\s*import\s+(?:static\s+)?", "", import_text).rstrip(";").strip()
                if not import_target:
                    continue
                symbol_id = f"ts:{relative_path}#{import_target}:import"
                kind = "Import"
                doc_symbols.append(
                    SymbolNode(
                        symbol_id=symbol_id,
                        display_name=import_target,
                        kind=kind,
                        package=package_name,
                        signature_hash=_sha1(symbol_id),
                        symbol_fingerprint=_fingerprint(symbol_id, import_target, kind),
                        enclosing_symbol=f"pkg:{package_name}" if package_name else "",
                        language="java",
                    )
                )
                sl, sc, el, ec = _range_from_node(import_node)
                doc_occurrences.append(
                    OccurrenceEdge(
                        document_id=document_id,
                        symbol_id=symbol_id,
                        range_start_line=sl,
                        range_start_col=sc,
                        range_end_line=el,
                        range_end_col=ec,
                        role="definition",
                        syntax_kind="import_declaration",
                    )
                )

            def queue_annotation_name_node(from_symbol: str, name_node: object, enclosing_node: object, syntax_kind: str) -> None:
                target_name = _annotation_name_text(_node_text(name_node, source_bytes))
                if not target_name:
                    return
                sl, sc, el, ec = _range_from_node(name_node)
                esl, esc, eel, eec = _range_from_node(enclosing_node)
                doc_pending_annotations.append(
                    _PendingAnnotation(
                        from_symbol=from_symbol,
                        target_name=target_name,
                        relative_path=relative_path,
                        package=package_name,
                        imports=imports,
                        range_start_line=sl,
                        range_start_col=sc,
                        range_end_line=el,
                        range_end_col=ec,
                        enclosing_range_start_line=esl,
                        enclosing_range_start_col=esc,
                        enclosing_range_end_line=eel,
                        enclosing_range_end_col=eec,
                        syntax_kind=syntax_kind,
                    )
                )

            def queue_declaration_annotations(from_symbol: str, declaration_node: object, syntax_kind: str) -> None:
                for name_node in _iter_declaration_annotation_name_nodes(declaration_node):
                    queue_annotation_name_node(from_symbol, name_node, declaration_node, syntax_kind)

            def queue_parameter_annotations(from_symbol: str, declaration_node: object, syntax_kind: str) -> None:
                for name_node in _iter_parameter_annotation_name_nodes(declaration_node):
                    parent = getattr(name_node, "parent", None) or declaration_node
                    queue_annotation_name_node(from_symbol, name_node, parent, syntax_kind)

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
                        "annotation_type_declaration": ("Annotation", "annotation"),
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
                        enclosing_symbol=owner.symbol_id if owner is not None else (f"pkg:{package_name}" if package_name else ""),
                        language="java",
                    )
                    type_symbol = _TypeSymbol(
                        symbol_id=symbol_id,
                        qualified_name=qualified_name,
                        simple_name=display_name,
                        package=package_name,
                        kind=kind_token[0],
                    )
                    doc_symbols.append(symbol)
                    doc_types.append(type_symbol)
                    sl, sc, el, ec = _range_from_node(name_node)
                    esl, esc, eel, eec = _range_from_node(node)
                    doc_occurrences.append(
                        OccurrenceEdge(
                            document_id=document_id,
                            symbol_id=symbol_id,
                            range_start_line=sl,
                            range_start_col=sc,
                            range_end_line=el,
                            range_end_col=ec,
                            role="definition",
                            syntax_kind=node_type,
                            enclosing_range_start_line=esl,
                            enclosing_range_start_col=esc,
                            enclosing_range_end_line=eel,
                            enclosing_range_end_col=eec,
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
                                imports=imports,
                                syntax_kind=node_type,
                            )
                        )
                    queue_declaration_annotations(symbol_id, node, "annotation")
                    child_by_field_name = getattr(node, "child_by_field_name", None)
                    super_node = child_by_field_name("superclass") if callable(child_by_field_name) else None
                    if super_node is None:
                        super_node = _child_by_type(node, "superclass")
                    if super_node is not None:
                        rsl, rsc, rel, rec = _range_from_node(super_node)
                        for target in _split_type_refs(_node_text(super_node, source_bytes)):
                            doc_pending.append(
                                _PendingRelation(
                                    from_symbol=symbol_id,
                                    relation_type="extends",
                                    target_name=target,
                                    relative_path=relative_path,
                                    package=package_name,
                                    imports=imports,
                                    range_start_line=rsl,
                                    range_start_col=rsc,
                                    range_end_line=rel,
                                    range_end_col=rec,
                                    syntax_kind="superclass",
                                )
                            )
                    for node_name, relation_type in (
                        ("super_interfaces", "implements"),
                        ("extends_interfaces", "extends"),
                    ):
                        intf_node = _child_by_type(node, node_name)
                        if intf_node is None:
                            continue
                        rsl, rsc, rel, rec = _range_from_node(intf_node)
                        for target in _split_type_refs(_node_text(intf_node, source_bytes)):
                            doc_pending.append(
                                _PendingRelation(
                                    from_symbol=symbol_id,
                                    relation_type=relation_type,
                                    target_name=target,
                                    relative_path=relative_path,
                                    package=package_name,
                                    imports=imports,
                                    range_start_line=rsl,
                                    range_start_col=rsc,
                                    range_end_line=rel,
                                    range_end_col=rec,
                                    syntax_kind=node_name,
                                )
                            )
                    body = child_by_field_name("body") if callable(child_by_field_name) else None
                    for child in _named_children(body or node):
                        if child is body:
                            continue
                        visit(child, [*enclosing, type_symbol])
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
                    doc_method = _MethodSymbol(
                        symbol_id=symbol_id,
                        owner_qualified_name=owner.qualified_name,
                        simple_name=display_name,
                        arity=arity,
                        package=package_name,
                        relative_path=relative_path,
                        is_constructor=True,
                    )
                    known_methods.append(doc_method)
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
                    esl, esc, eel, eec = _range_from_node(node)
                    doc_occurrences.append(
                        OccurrenceEdge(
                            document_id=document_id,
                            symbol_id=symbol_id,
                            range_start_line=sl,
                            range_start_col=sc,
                            range_end_line=el,
                            range_end_col=ec,
                            role="definition",
                            syntax_kind=node_type,
                            enclosing_range_start_line=esl,
                            enclosing_range_start_col=esc,
                            enclosing_range_end_line=eel,
                            enclosing_range_end_col=eec,
                        )
                    )
                    doc_pending.append(
                        _PendingRelation(
                            from_symbol=symbol_id,
                            relation_type="belongs_to",
                            target_name=owner.qualified_name,
                            relative_path=relative_path,
                            package=package_name,
                            imports=imports,
                            syntax_kind=node_type,
                        )
                    )
                    queue_declaration_annotations(symbol_id, node, "annotation")
                    queue_parameter_annotations(symbol_id, node, "parameter_annotation")
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
                    doc_method = _MethodSymbol(
                        symbol_id=symbol_id,
                        owner_qualified_name=owner.qualified_name,
                        simple_name=display_name,
                        arity=arity,
                        package=package_name,
                        relative_path=relative_path,
                    )
                    known_methods.append(doc_method)
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
                    esl, esc, eel, eec = _range_from_node(node)
                    doc_occurrences.append(
                        OccurrenceEdge(
                            document_id=document_id,
                            symbol_id=symbol_id,
                            range_start_line=sl,
                            range_start_col=sc,
                            range_end_line=el,
                            range_end_col=ec,
                            role="definition",
                            syntax_kind=node_type,
                            enclosing_range_start_line=esl,
                            enclosing_range_start_col=esc,
                            enclosing_range_end_line=eel,
                            enclosing_range_end_col=eec,
                        )
                    )
                    doc_pending.append(
                        _PendingRelation(
                            from_symbol=symbol_id,
                            relation_type="belongs_to",
                            target_name=owner.qualified_name,
                            relative_path=relative_path,
                            package=package_name,
                            imports=imports,
                            syntax_kind=node_type,
                        )
                    )
                    queue_declaration_annotations(symbol_id, node, "annotation")
                    queue_parameter_annotations(symbol_id, node, "parameter_annotation")
                    return

                if node_type == "field_declaration":
                    declared_type_node = _first_type_child(node)
                    declared_type = _type_text_from_node(declared_type_node, source_bytes)
                    for decl in _node_children_by_type(node, "variable_declarator"):
                        name_node = _find_name_node(decl)
                        if name_node is None:
                            continue
                        display_name = _node_text(name_node, source_bytes).strip()
                        if not display_name:
                            continue
                        symbol_id = f"ts:{relative_path}#{owner.qualified_name}.{display_name}:field"
                        kind = "Field"
                        known_fields.append(
                            _FieldSymbol(
                                symbol_id=symbol_id,
                                owner_qualified_name=owner.qualified_name,
                                simple_name=display_name,
                                package=package_name,
                                relative_path=relative_path,
                                declared_type=declared_type,
                            )
                        )
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
                        esl, esc, eel, eec = _range_from_node(node)
                        doc_occurrences.append(
                            OccurrenceEdge(
                                document_id=document_id,
                                symbol_id=symbol_id,
                                range_start_line=sl,
                                range_start_col=sc,
                                range_end_line=el,
                                range_end_col=ec,
                                role="definition",
                                syntax_kind=node_type,
                                enclosing_range_start_line=esl,
                                enclosing_range_start_col=esc,
                                enclosing_range_end_line=eel,
                                enclosing_range_end_col=eec,
                            )
                        )
                        doc_pending.append(
                            _PendingRelation(
                                from_symbol=symbol_id,
                                relation_type="belongs_to",
                                target_name=owner.qualified_name,
                                relative_path=relative_path,
                                package=package_name,
                                imports=imports,
                                syntax_kind=node_type,
                            )
                        )
                        if declared_type_node is not None:
                            rsl, rsc, rel, rec = _range_from_node(declared_type_node)
                            for target in _split_type_refs(declared_type):
                                doc_pending.append(
                                    _PendingRelation(
                                        from_symbol=symbol_id,
                                        relation_type="field_type",
                                        target_name=target,
                                        relative_path=relative_path,
                                        package=package_name,
                                        imports=imports,
                                        range_start_line=rsl,
                                        range_start_col=rsc,
                                        range_end_line=rel,
                                        range_end_col=rec,
                                        syntax_kind="field_type",
                                    )
                                )
                        queue_declaration_annotations(symbol_id, node, "annotation")
                    return

            for child in _named_children(tree.root_node):
                visit(child, [])

            documents.append(
                ScipDocument(
                    document_id=document_id,
                    relative_path=relative_path,
                    language="java",
                    occurrence_count=len(doc_occurrences),
                    content=content,
                )
            )
            symbols.extend(doc_symbols)
            occurrences.extend(doc_occurrences)
            pending.extend(doc_pending)
            pending_annotations.extend(doc_pending_annotations)
            known_types.extend(doc_types)

        by_qname = {item.qualified_name: item for item in known_types}
        by_package_and_simple: dict[tuple[str, str], list[_TypeSymbol]] = {}
        for item in known_types:
            by_package_and_simple.setdefault((item.package, item.simple_name), []).append(item)
        by_simple: dict[str, list[_TypeSymbol]] = {}
        for item in known_types:
            by_simple.setdefault(item.simple_name, []).append(item)

        methods_by_owner_name_arity: dict[tuple[str, str, int, bool], list[_MethodSymbol]] = {}
        for item in known_methods:
            methods_by_owner_name_arity.setdefault(
                (item.owner_qualified_name, item.simple_name, item.arity, item.is_constructor),
                [],
            ).append(item)

        fields_by_owner_name: dict[tuple[str, str], list[_FieldSymbol]] = {}
        for item in known_fields:
            fields_by_owner_name.setdefault((item.owner_qualified_name, item.simple_name), []).append(item)

        def resolve_type_obj(
            target_name: str,
            package_name: str,
            imports: _ImportContext | None = None,
        ) -> _TypeSymbol | None:
            raw = (target_name or "").strip()
            if not raw:
                return None
            raw = _strip_generics(raw).replace("[]", "").strip()
            if raw in by_qname:
                return by_qname[raw]
            short = raw.rsplit(".", 1)[-1]
            if imports is not None:
                imported = imports.direct.get(short)
                if imported and imported in by_qname:
                    return by_qname[imported]
            package_hits = by_package_and_simple.get((package_name, short), [])
            if len(package_hits) == 1:
                return package_hits[0]
            if imports is not None:
                wildcard_hits = [
                    by_qname[f"{pkg}.{short}"]
                    for pkg in imports.wildcard_packages
                    if f"{pkg}.{short}" in by_qname
                ]
                if len(wildcard_hits) == 1:
                    return wildcard_hits[0]
            candidates = by_simple.get(short, [])
            if len(candidates) == 1:
                return candidates[0]
            return None

        def resolve_type(target_name: str, package_name: str, imports: _ImportContext | None = None) -> str | None:
            typ = resolve_type_obj(target_name, package_name, imports)
            return typ.symbol_id if typ is not None else None

        seen_occurrences: set[tuple[str, str, int, int, str]] = {
            (o.document_id, o.symbol_id, o.range_start_line, o.range_start_col, o.role)
            for o in occurrences
        }
        seen_relations: set[tuple[str, str, str]] = set()

        def add_reference_range(
            *,
            document_id: str,
            target_symbol: str,
            range_start_line: int,
            range_start_col: int,
            range_end_line: int,
            range_end_col: int,
            syntax_kind: str,
            enclosing_range_start_line: int = -1,
            enclosing_range_start_col: int = -1,
            enclosing_range_end_line: int = -1,
            enclosing_range_end_col: int = -1,
        ) -> None:
            key = (document_id, target_symbol, range_start_line, range_start_col, "reference")
            if key in seen_occurrences:
                return
            seen_occurrences.add(key)
            occurrences.append(
                OccurrenceEdge(
                    document_id=document_id,
                    symbol_id=target_symbol,
                    range_start_line=range_start_line,
                    range_start_col=range_start_col,
                    range_end_line=range_end_line,
                    range_end_col=range_end_col,
                    role="reference",
                    syntax_kind=syntax_kind,
                    enclosing_range_start_line=enclosing_range_start_line,
                    enclosing_range_start_col=enclosing_range_start_col,
                    enclosing_range_end_line=enclosing_range_end_line,
                    enclosing_range_end_col=enclosing_range_end_col,
                )
            )

        def add_reference(
            *,
            document_id: str,
            target_symbol: str,
            node: object,
            syntax_kind: str,
            enclosing_node: object | None = None,
        ) -> None:
            sl, sc, el, ec = _range_from_node(node)
            kwargs: dict[str, int] = {
                "enclosing_range_start_line": -1,
                "enclosing_range_start_col": -1,
                "enclosing_range_end_line": -1,
                "enclosing_range_end_col": -1,
            }
            if enclosing_node is not None:
                esl, esc, eel, eec = _range_from_node(enclosing_node)
                kwargs = {
                    "enclosing_range_start_line": esl,
                    "enclosing_range_start_col": esc,
                    "enclosing_range_end_line": eel,
                    "enclosing_range_end_col": eec,
                }
            add_reference_range(
                document_id=document_id,
                target_symbol=target_symbol,
                range_start_line=sl,
                range_start_col=sc,
                range_end_line=el,
                range_end_col=ec,
                syntax_kind=syntax_kind,
                **kwargs,
            )

        def add_relation(
            *,
            from_symbol: str,
            to_symbol: str,
            relation_type: str,
            confidence: float,
            evidence_document_id: str,
        ) -> None:
            key = (from_symbol, to_symbol, relation_type)
            if key in seen_relations:
                return
            seen_relations.add(key)
            relations.append(
                RelationEdge(
                    from_symbol=from_symbol,
                    to_symbol=to_symbol,
                    relation_type=relation_type,
                    confidence=confidence,
                    evidence_document_id=evidence_document_id,
                )
            )

        for item in pending:
            target_symbol = resolve_type(item.target_name, item.package, item.imports)
            if target_symbol is None:
                continue
            document_id = f"{repo}:{commit}:{item.relative_path}"
            add_relation(
                from_symbol=item.from_symbol,
                to_symbol=target_symbol,
                relation_type=item.relation_type,
                confidence=1.0,
                evidence_document_id=document_id,
            )
            if item.range_start_line >= 0:
                occurrences.append(
                    OccurrenceEdge(
                        document_id=document_id,
                        symbol_id=target_symbol,
                        range_start_line=item.range_start_line,
                        range_start_col=item.range_start_col,
                        range_end_line=item.range_end_line,
                        range_end_col=item.range_end_col,
                        role="reference",
                        syntax_kind=item.syntax_kind,
                    )
                )

        synthetic_annotations: dict[str, SymbolNode] = {}

        def resolve_annotation_symbol(item: _PendingAnnotation) -> tuple[str, float] | None:
            typ = resolve_type_obj(item.target_name, item.package, item.imports)
            if typ is not None and typ.kind == "Annotation":
                return typ.symbol_id, 0.96
            qname = _best_effort_annotation_qname(item.target_name, item.package, item.imports)
            if not qname:
                return None
            short = qname.rsplit(".", 1)[-1]
            package = qname.rsplit(".", 1)[0] if "." in qname else item.package
            symbol_id = f"ann:{qname}"
            if symbol_id not in synthetic_annotations:
                synthetic_annotations[symbol_id] = SymbolNode(
                    symbol_id=symbol_id,
                    display_name=short,
                    kind="Annotation",
                    package=package,
                    signature_hash=_sha1(symbol_id),
                    symbol_fingerprint=_fingerprint(symbol_id, short, "Annotation"),
                    language="java",
                )
            return symbol_id, 0.82

        for item in pending_annotations:
            resolved_annotation = resolve_annotation_symbol(item)
            if resolved_annotation is None:
                continue
            target_symbol, confidence = resolved_annotation
            document_id = f"{repo}:{commit}:{item.relative_path}"
            add_reference_range(
                document_id=document_id,
                target_symbol=target_symbol,
                range_start_line=item.range_start_line,
                range_start_col=item.range_start_col,
                range_end_line=item.range_end_line,
                range_end_col=item.range_end_col,
                syntax_kind=item.syntax_kind,
                enclosing_range_start_line=item.enclosing_range_start_line,
                enclosing_range_start_col=item.enclosing_range_start_col,
                enclosing_range_end_line=item.enclosing_range_end_line,
                enclosing_range_end_col=item.enclosing_range_end_col,
            )
            add_relation(
                from_symbol=item.from_symbol,
                to_symbol=target_symbol,
                relation_type="annotated_with",
                confidence=confidence,
                evidence_document_id=document_id,
            )
        symbols.extend(synthetic_annotations.values())

        def resolve_method(
            owner_qname: str,
            name: str,
            arity: int,
            *,
            constructor: bool = False,
        ) -> _MethodSymbol | None:
            candidates = methods_by_owner_name_arity.get((owner_qname, name, arity, constructor), [])
            return candidates[0] if len(candidates) == 1 else None

        def resolve_field(owner_qname: str, name: str) -> _FieldSymbol | None:
            candidates = fields_by_owner_name.get((owner_qname, name), [])
            return candidates[0] if len(candidates) == 1 else None

        def field_type_qname(field: _FieldSymbol, imports: _ImportContext) -> str | None:
            typ = resolve_type_obj(field.declared_type, field.package, imports)
            return typ.qualified_name if typ is not None else None

        def scan_references_for_document(path: Path, relative_path: str) -> None:
            content = path.read_text(encoding="utf-8", errors="replace")
            source_bytes = content.encode("utf-8")
            tree = parser.parse(source_bytes)
            package_name = ""
            package_node = _child_by_type(tree.root_node, "package_declaration")
            if package_node is not None:
                package_name = _node_text(package_node, source_bytes)
                package_name = package_name.replace("package", "").replace(";", "").strip()
            imports = doc_imports.get(relative_path, _ImportContext(direct={}, wildcard_packages=()))
            document_id = f"{repo}:{commit}:{relative_path}"

            def collect_locals(method_node: object) -> tuple[dict[str, str], set[str]]:
                local_types: dict[str, str] = {}
                local_names: set[str] = set()
                for candidate in _walk(method_node):
                    ctype = str(getattr(candidate, "type", ""))
                    if ctype in {"formal_parameter", "spread_parameter", "catch_formal_parameter"}:
                        type_node = _child_by_field_name(candidate, "type")
                        name_node = _find_name_node(candidate)
                        name = _node_text(name_node, source_bytes).strip() if name_node is not None else ""
                        if name:
                            local_names.add(name)
                        typ = resolve_type_obj(_type_text_from_node(type_node, source_bytes), package_name, imports)
                        if name and typ is not None:
                            local_types[name] = typ.qualified_name
                    elif ctype == "local_variable_declaration":
                        type_node = _child_by_field_name(candidate, "type")
                        typ = resolve_type_obj(_type_text_from_node(type_node, source_bytes), package_name, imports)
                        for decl in _node_children_by_type(candidate, "variable_declarator"):
                            name_node = _find_name_node(decl)
                            name = _node_text(name_node, source_bytes).strip() if name_node is not None else ""
                            if not name:
                                continue
                            local_names.add(name)
                            if typ is not None:
                                local_types[name] = typ.qualified_name
                return local_types, local_names

            def add_field_ref(
                current_method: _MethodSymbol,
                field: _FieldSymbol,
                node: object,
                method_node: object,
            ) -> None:
                add_reference(
                    document_id=document_id,
                    target_symbol=field.symbol_id,
                    node=node,
                    syntax_kind="field_reference",
                    enclosing_node=method_node,
                )
                add_relation(
                    from_symbol=current_method.symbol_id,
                    to_symbol=field.symbol_id,
                    relation_type="field_refs",
                    confidence=0.72,
                    evidence_document_id=document_id,
                )

            def scan(
                node: object,
                enclosing: Sequence[_TypeSymbol],
                current_method: _MethodSymbol | None,
                current_method_node: object | None,
                local_types: Mapping[str, str],
                local_names: AbstractSet[str],
            ) -> None:
                node_type = str(getattr(node, "type", ""))
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
                    qname_parts = [part for part in [package_name, *(x.simple_name for x in enclosing), display_name] if part]
                    typ = by_qname.get(".".join(qname_parts))
                    if typ is None:
                        return
                    body = _child_by_field_name(node, "body")
                    for child in _named_children(body or node):
                        if child is body:
                            continue
                        scan(child, [*enclosing, typ], None, None, {}, set())
                    return

                owner = enclosing[-1] if enclosing else None
                if owner is not None and node_type in {"method_declaration", "constructor_declaration"}:
                    name_node = _find_name_node(node)
                    if name_node is None:
                        return
                    arity = _count_parameters(node)
                    is_constructor = node_type == "constructor_declaration"
                    name = owner.simple_name if is_constructor else _node_text(name_node, source_bytes).strip()
                    method = resolve_method(owner.qualified_name, name, arity, constructor=is_constructor)
                    if method is None:
                        return
                    locals_for_method, local_names_for_method = collect_locals(node)
                    for child in _named_children(node):
                        scan(child, enclosing, method, node, locals_for_method, local_names_for_method)
                    return

                if current_method is not None and owner is not None and current_method_node is not None:
                    if node_type == "method_invocation":
                        name_node = _child_by_field_name(node, "name") or _find_name_node(node)
                        if name_node is not None:
                            name = _node_text(name_node, source_bytes).strip()
                            receiver_node = _child_by_field_name(node, "object")
                            receiver = _node_text(receiver_node, source_bytes).strip() if receiver_node is not None else ""
                            owner_qname = owner.qualified_name
                            confidence = 0.66
                            if receiver in {"", "this"}:
                                owner_qname = owner.qualified_name
                            elif receiver in local_types:
                                owner_qname = str(local_types[receiver])
                                confidence = 0.78
                            elif receiver in local_names:
                                owner_qname = ""
                            else:
                                receiver_short = receiver.split(".")[-1]
                                field = resolve_field(owner.qualified_name, receiver_short)
                                if field is not None and receiver_node is not None:
                                    add_field_ref(current_method, field, receiver_node, current_method_node)
                                    field_type = field_type_qname(field, imports)
                                    if field_type is not None:
                                        owner_qname = field_type
                                        confidence = 0.74
                                else:
                                    typ = resolve_type_obj(receiver, package_name, imports)
                                    if typ is not None:
                                        owner_qname = typ.qualified_name
                                        confidence = 0.78
                            target = resolve_method(owner_qname, name, _argument_count(node)) if owner_qname else None
                            if target is not None:
                                add_reference(
                                    document_id=document_id,
                                    target_symbol=target.symbol_id,
                                    node=name_node,
                                    syntax_kind="method_invocation",
                                    enclosing_node=current_method_node,
                                )
                                add_relation(
                                    from_symbol=current_method.symbol_id,
                                    to_symbol=target.symbol_id,
                                    relation_type="calls",
                                    confidence=confidence,
                                    evidence_document_id=document_id,
                                )
                    elif node_type == "object_creation_expression":
                        type_node = _first_type_child(node)
                        typ = resolve_type_obj(_type_text_from_node(type_node, source_bytes), package_name, imports)
                        if typ is not None and type_node is not None:
                            target = resolve_method(
                                typ.qualified_name,
                                typ.simple_name,
                                _argument_count(node),
                                constructor=True,
                            )
                            if target is not None:
                                add_reference(
                                    document_id=document_id,
                                    target_symbol=target.symbol_id,
                                    node=type_node,
                                    syntax_kind="constructor_invocation",
                                    enclosing_node=current_method_node,
                                )
                                add_relation(
                                    from_symbol=current_method.symbol_id,
                                    to_symbol=target.symbol_id,
                                    relation_type="calls",
                                    confidence=0.78,
                                    evidence_document_id=document_id,
                                )
                    elif node_type == "field_access":
                        name_node = _child_by_field_name(node, "field") or _find_name_node(node)
                        receiver_node = _child_by_field_name(node, "object")
                        receiver = _node_text(receiver_node, source_bytes).strip() if receiver_node is not None else ""
                        if name_node is not None and receiver in {"", "this"}:
                            field = resolve_field(owner.qualified_name, _node_text(name_node, source_bytes).strip())
                            if field is not None:
                                add_field_ref(current_method, field, name_node, current_method_node)
                    elif node_type == "identifier":
                        name = _node_text(node, source_bytes).strip()
                        parent = getattr(node, "parent", None)
                        parent_type = str(getattr(parent, "type", ""))
                        if (
                            name
                            and name not in local_names
                            and not _is_child_field(parent, "name", node)
                            and parent_type not in {"variable_declarator", "formal_parameter", "catch_formal_parameter"}
                        ):
                            field = resolve_field(owner.qualified_name, name)
                            if field is not None:
                                add_field_ref(current_method, field, node, current_method_node)

                for child in _named_children(node):
                    scan(child, enclosing, current_method, current_method_node, local_types, local_names)

            for child in _named_children(tree.root_node):
                scan(child, [], None, None, {}, set())

        for path in _iter_java_files(repo_root):
            relative_path = str(path.resolve().relative_to(root))
            scan_references_for_document(path, relative_path)

        symbols = list({item.symbol_id: item for item in symbols}.values())
        occurrence_counts = Counter(item.document_id for item in occurrences)
        documents = [
            ScipDocument(
                document_id=doc.document_id,
                relative_path=doc.relative_path,
                language=doc.language,
                occurrence_count=int(occurrence_counts.get(doc.document_id, doc.occurrence_count)),
                content=doc.content,
            )
            for doc in documents
        ]

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
