from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

INDEX_SCHEMA_VERSION = "v2"

SOURCE_MODE_SCIP = "scip"
SOURCE_MODE_SYNTAX = "syntax"
SOURCE_MODE_DOCUMENT = "document"

VALID_SOURCE_MODES = {
    SOURCE_MODE_SCIP,
    SOURCE_MODE_SYNTAX,
    SOURCE_MODE_DOCUMENT,
}

SOURCE_BACKEND_SCIP_JAVA = "scip-java"
SOURCE_BACKEND_TREE_SITTER_JAVA = "tree-sitter-java"
SOURCE_BACKEND_DOCUMENT = "document"

VALID_SOURCE_BACKENDS = {
    SOURCE_BACKEND_SCIP_JAVA,
    SOURCE_BACKEND_TREE_SITTER_JAVA,
    SOURCE_BACKEND_DOCUMENT,
}

FALLBACK_MODE_OFF = "off"
FALLBACK_MODE_SYNTAX = "syntax"
FALLBACK_MODE_DOCUMENT = "document"

VALID_FALLBACK_MODES = {
    FALLBACK_MODE_OFF,
    FALLBACK_MODE_SYNTAX,
    FALLBACK_MODE_DOCUMENT,
}

CAP_FIND_ENTITY = "find_entity"
CAP_DEF = "def"
CAP_REF = "ref"
CAP_CALL = "call"
CAP_HIERARCHY = "hierarchy"
CAP_KEYWORD = "keyword"
CAP_HYBRID = "hybrid"
CAP_SEMANTIC = "semantic"

CAPABILITIES_BY_SOURCE_MODE = {
    SOURCE_MODE_SCIP: (
        CAP_FIND_ENTITY,
        CAP_DEF,
        CAP_REF,
        CAP_CALL,
        CAP_HIERARCHY,
        CAP_KEYWORD,
        CAP_HYBRID,
        CAP_SEMANTIC,
    ),
    SOURCE_MODE_SYNTAX: (
        CAP_FIND_ENTITY,
        CAP_DEF,
        CAP_REF,
        CAP_CALL,
        CAP_HIERARCHY,
        CAP_KEYWORD,
        CAP_HYBRID,
        CAP_SEMANTIC,
    ),
    SOURCE_MODE_DOCUMENT: (
        CAP_KEYWORD,
        CAP_HYBRID,
        CAP_SEMANTIC,
    ),
}


def normalize_source_mode(source_mode: str) -> str:
    mode = (source_mode or "").strip().lower()
    if mode not in VALID_SOURCE_MODES:
        raise ValueError(f"unsupported source_mode: {source_mode!r}")
    return mode


def normalize_source_backend(source_backend: str) -> str:
    backend = (source_backend or "").strip().lower()
    if backend not in VALID_SOURCE_BACKENDS:
        allowed = ", ".join(sorted(VALID_SOURCE_BACKENDS))
        raise ValueError(f"unsupported source_backend: {source_backend!r}; allowed: {allowed}")
    return backend


def default_source_backend_for_mode(source_mode: str) -> str:
    mode = normalize_source_mode(source_mode)
    if mode == SOURCE_MODE_SCIP:
        return SOURCE_BACKEND_SCIP_JAVA
    if mode == SOURCE_MODE_SYNTAX:
        return SOURCE_BACKEND_TREE_SITTER_JAVA
    return SOURCE_BACKEND_DOCUMENT


def normalize_fallback_mode(fallback_mode: str) -> str:
    mode = (fallback_mode or "").strip().lower()
    if mode not in VALID_FALLBACK_MODES:
        raise ValueError(f"unsupported fallback_mode: {fallback_mode!r}")
    return mode


def capabilities_for_source_mode(source_mode: str) -> list[str]:
    mode = normalize_source_mode(source_mode)
    return sorted(str(x) for x in CAPABILITIES_BY_SOURCE_MODE[mode])


@dataclass(frozen=True)
class IndexInfo:
    repo: str
    commit_hash: str
    schema_version: str
    source_mode: str
    capabilities: tuple[str, ...]
    build_tool: str
    build_failure_json: str
    source_backend: str
    backend_version: str
    backend_stats_json: str
    created_at_epoch_ms: int


class IndexContractError(RuntimeError):
    pass


class ReindexRequiredError(IndexContractError):
    pass


class SnapshotMismatchError(IndexContractError):
    pass


class UnsupportedCapabilityError(RuntimeError):
    def __init__(self, capability: str, source_mode: str, *, detail: str = "") -> None:
        self.capability = str(capability)
        self.source_mode = str(source_mode or "")
        message = f"unsupported_capability: {self.capability} is not available for source_mode={self.source_mode or 'unknown'}"
        if detail:
            message = f"{message} ({detail})"
        super().__init__(message)


def ensure_capability(capabilities: Iterable[str], capability: str, source_mode: str) -> None:
    cap = str(capability)
    if cap in {str(x) for x in capabilities}:
        return
    raise UnsupportedCapabilityError(cap, source_mode)
