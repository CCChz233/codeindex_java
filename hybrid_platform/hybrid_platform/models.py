from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass(frozen=True)
class RepoSnapshot:
    repo: str
    commit: str
    index_version: str
    ingested_at_epoch_ms: int


@dataclass(frozen=True)
class ScipDocument:
    document_id: str
    relative_path: str
    language: str
    occurrence_count: int
    content: str = ""


@dataclass(frozen=True)
class SymbolNode:
    symbol_id: str
    display_name: str
    kind: str
    package: str
    signature_hash: str
    symbol_fingerprint: str
    enclosing_symbol: str = ""
    language: str = ""


@dataclass(frozen=True)
class OccurrenceEdge:
    document_id: str
    symbol_id: str
    range_start_line: int
    range_start_col: int
    range_end_line: int
    range_end_col: int
    role: str
    syntax_kind: str = ""
    enclosing_range_start_line: int = -1
    enclosing_range_start_col: int = -1
    enclosing_range_end_line: int = -1
    enclosing_range_end_col: int = -1


@dataclass(frozen=True)
class RelationEdge:
    from_symbol: str
    to_symbol: str
    relation_type: str
    confidence: float
    evidence_document_id: str


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    document_id: str
    content: str
    primary_symbol_ids: List[str]
    span_start_line: int
    span_end_line: int
    embedding_version: str = ""


@dataclass
class QueryResult:
    result_id: str
    result_type: str
    score: float
    explain: Dict[str, float] = field(default_factory=dict)
    payload: Optional[Dict[str, object]] = None
