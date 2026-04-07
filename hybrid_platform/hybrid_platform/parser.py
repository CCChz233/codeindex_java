from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Iterable, Iterator, Tuple

from .models import OccurrenceEdge, RelationEdge, ScipDocument, SymbolNode


def _fingerprint(symbol_id: str, display_name: str, kind: str) -> str:
    payload = f"{symbol_id}|{display_name}|{kind}".encode("utf-8")
    return hashlib.sha1(payload).hexdigest()


def _infer_language(relative_path: str, raw_language: str) -> str:
    value = (raw_language or "").strip().lower()
    if value:
        return value
    suffix = Path(relative_path).suffix.lower()
    if suffix == ".java":
        return "java"
    if suffix in {".kt", ".kts"}:
        return "kotlin"
    if suffix == ".scala":
        return "scala"
    if suffix in {".cc", ".cpp", ".cxx", ".hpp", ".hh", ".h"}:
        return "cpp"
    return ""


def _semanticdb_descriptor(symbol_id: str) -> str:
    parts = symbol_id.split(" ", 4)
    if len(parts) == 5:
        return parts[4]
    return ""


def _infer_package_path(symbol_id: str) -> str:
    desc = _semanticdb_descriptor(symbol_id)
    if not desc:
        return ""
    if desc.endswith("/"):
        return desc.rstrip("/")
    if "/" not in desc:
        return ""
    return desc.rsplit("/", 1)[0]


def _infer_enclosing_symbol(symbol_id: str, explicit: str) -> str:
    """Infer enclosing_symbol from symbol_id URI. SCIP/SemanticDB often omit this field.
    For Method/Field/Constructor: enclosing = Class# or Interface# (parent type).
    For Class/Interface: leave empty so code_graph uses package path for belongs_to."""
    if explicit:
        return explicit
    if symbol_id.startswith("local "):
        return ""
    desc = _semanticdb_descriptor(symbol_id)
    if not desc or desc.endswith("/"):
        return ""
    hash_idx = desc.find("#")
    if hash_idx == -1:
        return ""
    remainder = desc[hash_idx + 1 :]
    if remainder:
        base = desc[: hash_idx + 1]
        prefix = symbol_id[: len(symbol_id) - len(desc)]
        return f"{prefix}{base}"
    return ""


def _iter_ndjson(path: Path) -> Iterator[dict]:
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            raw = line.strip()
            if not raw:
                continue
            try:
                yield json.loads(raw)
            except json.JSONDecodeError:
                # 单行容错，避免单条坏数据阻断全文件 ingest。
                continue


def _normalize_range(raw_range: object) -> list[int]:
    rng = [int(x) for x in list(raw_range or [])]
    if len(rng) == 3:
        return [rng[0], rng[1], rng[0], rng[2]]
    if len(rng) == 4:
        return rng
    return [0, 0, 0, 0]


def _normalize_enclosing_range(raw_range: object) -> list[int]:
    rng = [int(x) for x in list(raw_range or [])]
    if len(rng) == 3:
        return [rng[0], rng[1], rng[0], rng[2]]
    if len(rng) == 4:
        return rng
    return [-1, -1, -1, -1]


def _from_ndjson(path: Path, repo: str, commit: str) -> Iterable[Tuple[str, object]]:
    for row in _iter_ndjson(path):
        record_type = row.get("type", "")
        if record_type == "document":
            rel = row["relative_path"]
            doc_id = f"{repo}:{commit}:{rel}"
            yield "document", ScipDocument(
                document_id=doc_id,
                relative_path=rel,
                language=_infer_language(rel, row.get("language", "")),
                occurrence_count=int(row.get("occurrence_count", 0)),
                content=row.get("content", ""),
            )
        elif record_type == "symbol":
            symbol_id = row["symbol_id"]
            display_name = row.get("display_name", symbol_id)
            kind = row.get("kind", "unknown")
            enclosing_symbol = _infer_enclosing_symbol(symbol_id, row.get("enclosing_symbol", ""))
            yield "symbol", SymbolNode(
                symbol_id=symbol_id,
                display_name=display_name,
                kind=kind,
                package=row.get("package", "") or _infer_package_path(symbol_id),
                signature_hash=row.get("signature_hash", ""),
                symbol_fingerprint=_fingerprint(symbol_id, display_name, kind),
                enclosing_symbol=enclosing_symbol,
                language=_infer_language(row.get("relative_path", ""), row.get("language", "")),
            )
        elif record_type == "occurrence":
            rel = row["relative_path"]
            doc_id = f"{repo}:{commit}:{rel}"
            r = _normalize_range(row.get("range", [0, 0, 0, 0]))
            er = _normalize_enclosing_range(row.get("enclosing_range", []))
            yield "occurrence", OccurrenceEdge(
                document_id=doc_id,
                symbol_id=row["symbol_id"],
                range_start_line=int(r[0]),
                range_start_col=int(r[1]),
                range_end_line=int(r[2]),
                range_end_col=int(r[3]),
                role=row.get("role", "unknown"),
                syntax_kind=str(row.get("syntax_kind", "")),
                enclosing_range_start_line=int(er[0]),
                enclosing_range_start_col=int(er[1]),
                enclosing_range_end_line=int(er[2]),
                enclosing_range_end_col=int(er[3]),
            )
        elif record_type == "relation":
            rel = row.get("evidence_relative_path", "")
            doc_id = f"{repo}:{commit}:{rel}" if rel else ""
            yield "relation", RelationEdge(
                from_symbol=row["from_symbol"],
                to_symbol=row["to_symbol"],
                relation_type=row.get("relation_type", "references"),
                confidence=float(row.get("confidence", 1.0)),
                evidence_document_id=doc_id,
            )


def _from_binary_scip(path: Path, repo: str, commit: str, source_root: str | None = None) -> Iterable[Tuple[str, object]]:
    scip_pb2 = None
    try:
        import scip_pb2  # type: ignore

        scip_pb2 = scip_pb2
    except Exception:
        pass
    if scip_pb2 is None:
        try:
            from scip import scip_pb2 as scip_pb2_module  # type: ignore

            scip_pb2 = scip_pb2_module
        except Exception as exc:
            raise RuntimeError(
                "binary .scip parsing requires scip_pb2 or scip.scip_pb2; "
                "or use ndjson input."
            ) from exc

    index = scip_pb2.Index()
    index.ParseFromString(path.read_bytes())
    root = source_root or str(Path.cwd())
    source_root_path = Path(root)
    for document in index.documents:
        doc_id = f"{repo}:{commit}:{document.relative_path}"
        content = ""
        source_path = source_root_path / document.relative_path
        if source_path.exists():
            try:
                content = source_path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                content = ""
        yield "document", ScipDocument(
            document_id=doc_id,
            relative_path=document.relative_path,
            language=_infer_language(document.relative_path, getattr(document, "language", "")),
            occurrence_count=len(document.occurrences),
            content=content,
        )
        for occ in document.occurrences:
            rng = _normalize_range(occ.range)
            enclosing_rng = _normalize_enclosing_range(getattr(occ, "enclosing_range", []))
            # scip-java often leaves non-definition occurrences with a zero bitmask,
            # so we treat any non-definition occurrence as a reference by default.
            role = "reference"
            if occ.symbol_roles & 1:
                role = "definition"
            syntax_kind = ""
            try:
                syntax_kind = str(scip_pb2.SyntaxKind.Name(int(getattr(occ, "syntax_kind", 0))))
            except Exception:
                syntax_kind = str(getattr(occ, "syntax_kind", ""))
            yield "occurrence", OccurrenceEdge(
                document_id=doc_id,
                symbol_id=occ.symbol,
                range_start_line=int(rng[0]),
                range_start_col=int(rng[1]),
                range_end_line=int(rng[2]),
                range_end_col=int(rng[3]),
                role=role,
                syntax_kind=syntax_kind,
                enclosing_range_start_line=int(enclosing_rng[0]),
                enclosing_range_start_col=int(enclosing_rng[1]),
                enclosing_range_end_line=int(enclosing_rng[2]),
                enclosing_range_end_col=int(enclosing_rng[3]),
            )
        for info in document.symbols:
            symbol_id = info.symbol
            display_name = info.display_name or symbol_id
            try:
                kind = str(scip_pb2.SymbolInformation.Kind.Name(int(info.kind)))
            except Exception:
                kind = str(info.kind if hasattr(info, "kind") else "unknown")
            enclosing_symbol = _infer_enclosing_symbol(symbol_id, getattr(info, "enclosing_symbol", ""))
            yield "symbol", SymbolNode(
                symbol_id=symbol_id,
                display_name=display_name,
                kind=str(kind),
                package=_infer_package_path(symbol_id),
                signature_hash=hashlib.sha1(symbol_id.encode("utf-8")).hexdigest(),
                symbol_fingerprint=_fingerprint(symbol_id, display_name, str(kind)),
                enclosing_symbol=enclosing_symbol,
                language=_infer_language(document.relative_path, getattr(document, "language", "")),
            )
            for rel in info.relationships:
                relation_type = "references"
                if getattr(rel, "is_reference", False):
                    relation_type = "references"
                elif getattr(rel, "is_implementation", False):
                    relation_type = "implements"
                elif getattr(rel, "is_type_definition", False):
                    relation_type = "extends"
                yield "relation", RelationEdge(
                    from_symbol=symbol_id,
                    to_symbol=rel.symbol,
                    relation_type=relation_type,
                    confidence=1.0,
                    evidence_document_id=doc_id,
                )


def parse_scip_stream(path: str, repo: str, commit: str, source_root: str | None = None) -> Iterable[Tuple[str, object]]:
    p = Path(path)
    suffix = p.suffix.lower()
    if suffix in {".ndjson", ".jsonl"} or p.name.endswith(".scip.ndjson"):
        return _from_ndjson(p, repo, commit)
    if suffix == ".scip":
        return _from_binary_scip(p, repo, commit, source_root=source_root)
    raise ValueError(f"unsupported input file: {path}")
