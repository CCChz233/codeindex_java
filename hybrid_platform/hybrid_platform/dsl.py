from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict


VALID_MODES = {"structure", "semantic", "hybrid"}
VALID_BLEND = {"linear", "rrf"}
VALID_STRUCTURED_OPS = {"search", "symbol_exact", "def_of", "refs_of", "callers_of", "callees_of"}


@dataclass(frozen=True)
class Query:
    text: str
    mode: str = "hybrid"
    top_k: int = 10
    filters: Dict[str, str] = field(default_factory=dict)
    blend_strategy: str = "linear"
    structured_op: str = "search"
    symbol_id: str = ""

    def validate(self) -> None:
        if self.structured_op not in VALID_STRUCTURED_OPS:
            raise ValueError(f"unsupported structured op: {self.structured_op}")
        if self.mode not in VALID_MODES:
            raise ValueError(f"unsupported mode: {self.mode}")
        if self.blend_strategy not in VALID_BLEND:
            raise ValueError(f"unsupported blend strategy: {self.blend_strategy}")
        if self.top_k <= 0:
            raise ValueError("top_k must be > 0")
        if self.structured_op in {"search", "symbol_exact"}:
            if not self.text.strip():
                raise ValueError("query text must not be empty")
            return
        if self.mode != "structure":
            raise ValueError("structured symbol operations must use structure mode")
        if not self.symbol_id.strip():
            raise ValueError("symbol_id must not be empty")


def symbol_exact(name: str, **filters: str) -> Query:
    return Query(text=name, mode="structure", filters=filters, structured_op="symbol_exact")


def def_of(symbol_id: str, top_k: int = 10) -> Query:
    return Query(text=f"def:{symbol_id}", mode="structure", top_k=top_k, structured_op="def_of", symbol_id=symbol_id)


def refs_of(symbol_id: str, top_k: int = 10) -> Query:
    return Query(text=f"refs:{symbol_id}", mode="structure", top_k=top_k, structured_op="refs_of", symbol_id=symbol_id)


def callers_of(symbol_id: str, top_k: int = 10) -> Query:
    return Query(text=f"callers:{symbol_id}", mode="structure", top_k=top_k, structured_op="callers_of", symbol_id=symbol_id)


def callees_of(symbol_id: str, top_k: int = 10) -> Query:
    return Query(text=f"callees:{symbol_id}", mode="structure", top_k=top_k, structured_op="callees_of", symbol_id=symbol_id)


def semantic_text(query: str, top_k: int = 10, **filters: str) -> Query:
    return Query(text=query, mode="semantic", top_k=top_k, filters=filters)


def hybrid(query: str, top_k: int = 10, blend_strategy: str = "linear", **filters: str) -> Query:
    return Query(text=query, mode="hybrid", top_k=top_k, blend_strategy=blend_strategy, filters=filters)
