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
