from __future__ import annotations

from collections import defaultdict
from typing import Dict, List

from .dsl import Query
from .embedding import EmbeddingPipeline
from .models import QueryResult
from .query_test_signals import (
    path_looks_like_test_source,
    query_implies_test_intent,
    symbol_id_suggests_test_path,
)
from .storage import SqliteStore


def _apply_test_depref_to_score(score: float, factor: float) -> float:
    """在「分数越大越好」的排序下削弱测试命中：正分乘 factor；负分除 factor（更负），避免 Lance 语义分（-distance）乘 factor 反而更靠前。"""
    if factor >= 0.999:
        return score
    if score >= 0.0:
        return score * factor
    return score / factor


class HybridRetrievalService:
    def __init__(
        self,
        store: SqliteStore,
        embedding_pipeline: EmbeddingPipeline | None = None,
        default_embedding_version: str = "v1",
        *,
        test_code_depref_enabled: bool = True,
        test_code_score_factor: float = 0.55,
    ) -> None:
        self.store = store
        self.embedding_pipeline = embedding_pipeline or EmbeddingPipeline(store)
        self.default_embedding_version = default_embedding_version
        self.test_code_depref_enabled = bool(test_code_depref_enabled)
        self.test_code_score_factor = max(0.0, min(1.0, float(test_code_score_factor)))

    def def_of(self, symbol_id: str, top_k: int = 10) -> List[QueryResult]:
        return self.store.def_of(symbol_id, top_k)

    def refs_of(self, symbol_id: str, top_k: int = 10) -> List[QueryResult]:
        return self.store.refs_of(symbol_id, top_k)

    def callers_of(self, symbol_id: str, top_k: int = 10) -> List[QueryResult]:
        return self.store.callers_of(symbol_id, top_k)

    def callees_of(self, symbol_id: str, top_k: int = 10) -> List[QueryResult]:
        return self.store.callees_of(symbol_id, top_k)

    def _linear_fusion(
        self,
        structured: List[QueryResult],
        keyword: List[QueryResult],
        semantic: List[QueryResult],
    ) -> List[QueryResult]:
        merged: Dict[str, QueryResult] = {}
        weights = {"structure": 0.5, "keyword": 0.2, "semantic": 0.3}
        for source_name, source in [
            ("structure", structured),
            ("keyword", keyword),
            ("semantic", semantic),
        ]:
            for rank, item in enumerate(source, start=1):
                base = merged.get(item.result_id)
                score = item.score * weights[source_name]
                if base is None:
                    merged[item.result_id] = QueryResult(
                        result_id=item.result_id,
                        result_type=item.result_type,
                        score=score,
                        explain={source_name: item.score},
                        payload=item.payload,
                    )
                else:
                    base.score += score
                    base.explain[source_name] = item.score
                    if not base.payload and item.payload:
                        base.payload = item.payload
                _ = rank
        ranked = list(merged.values())
        ranked.sort(key=lambda x: x.score, reverse=True)
        return ranked

    def _rrf_fusion(
        self,
        structured: List[QueryResult],
        keyword: List[QueryResult],
        semantic: List[QueryResult],
        k: int = 60,
    ) -> List[QueryResult]:
        rank_map: Dict[str, float] = defaultdict(float)
        payload_map: Dict[str, QueryResult] = {}
        for name, source in [("structure", structured), ("keyword", keyword), ("semantic", semantic)]:
            for rank, item in enumerate(source, start=1):
                rank_map[item.result_id] += 1.0 / (k + rank)
                payload_map.setdefault(item.result_id, item)
                payload_map[item.result_id].explain[name] = item.score
        ranked = [
            QueryResult(
                result_id=item_id,
                result_type=payload_map[item_id].result_type,
                score=score,
                explain=payload_map[item_id].explain,
                payload=payload_map[item_id].payload,
            )
            for item_id, score in rank_map.items()
        ]
        ranked.sort(key=lambda x: x.score, reverse=True)
        return ranked

    @staticmethod
    def _truncate_text(text: str, max_code_chars: int) -> tuple[str, bool]:
        if len(text) <= max_code_chars:
            return text, False
        return text[:max_code_chars], True

    def _skip_test_depref_for_query(self, q: Query) -> bool:
        """自然语言或结构化查询 seed 已显式落在测试域时，不做降权。"""
        if query_implies_test_intent(q.text or ""):
            return True
        sid = getattr(q, "symbol_id", None) or ""
        if sid and symbol_id_suggests_test_path(str(sid)):
            return True
        return False

    def _resolve_path_for_result(self, item: QueryResult) -> str | None:
        pl = item.payload or {}
        p = pl.get("path")
        if isinstance(p, str) and p.strip():
            return p.strip()
        if item.result_type == "chunk":
            meta = self.store.fetch_chunk_metadata(item.result_id, include_content=False)
            if meta:
                path = meta.get("path")
                if isinstance(path, str) and path.strip():
                    return path.strip()
        elif item.result_type == "symbol":
            rp = self.store.fetch_relative_path_for_symbol(item.result_id)
            if rp and rp.strip():
                return rp.strip()
        return None

    def _result_likely_test_code(self, item: QueryResult) -> bool:
        path = self._resolve_path_for_result(item)
        if path and path_looks_like_test_source(path):
            return True
        if item.result_type == "symbol" and symbol_id_suggests_test_path(item.result_id):
            return True
        return False

    def _rerank_with_test_depref(self, q: Query, results: List[QueryResult]) -> List[QueryResult]:
        """融合后对疑似测试代码削弱最终 score 并重排（除非查询显式关注测试）。"""
        if not self.test_code_depref_enabled:
            return results
        if self._skip_test_depref_for_query(q):
            return results
        factor = self.test_code_score_factor
        if factor >= 0.999:
            return results
        out: List[QueryResult] = []
        for item in results:
            if self._result_likely_test_code(item):
                ex = dict(item.explain)
                ex["test_depref_factor"] = factor
                new_score = _apply_test_depref_to_score(item.score, factor)
                out.append(
                    QueryResult(
                        result_id=item.result_id,
                        result_type=item.result_type,
                        score=new_score,
                        explain=ex,
                        payload=item.payload,
                    )
                )
            else:
                out.append(item)
        out.sort(key=lambda x: x.score, reverse=True)
        return out

    def _attach_code(
        self,
        results: List[QueryResult],
        include_code: bool,
        max_code_chars: int,
    ) -> List[QueryResult]:
        if not include_code:
            return results
        for item in results:
            payload = dict(item.payload or {})
            if item.result_type == "chunk":
                meta = self.store.fetch_chunk_metadata(item.result_id, include_content=True)
                if meta:
                    code_raw = str(meta.get("content", ""))
                    code, truncated = self._truncate_text(code_raw, max_code_chars)
                    payload.update(
                        {
                            "path": meta["path"],
                            "document_id": meta["document_id"],
                            "language": meta["language"],
                            "start_line": meta["start_line"],
                            "end_line": meta["end_line"],
                            "code": code,
                            "truncated": truncated,
                        }
                    )
            elif item.result_type == "symbol":
                snippet = self.store.fetch_symbol_definition_snippet(item.result_id)
                if snippet:
                    code_raw = str(snippet.get("code", ""))
                    code, truncated = self._truncate_text(code_raw, max_code_chars)
                    payload.update(
                        {
                            "path": snippet["path"],
                            "language": snippet["language"],
                            "start_line": snippet["start_line"],
                            "end_line": snippet["end_line"],
                            "code": code,
                            "truncated": truncated,
                        }
                    )
            item.payload = payload or None
        return results

    def query(
        self,
        q: Query,
        embedding_version: str | None = None,
        include_code: bool = False,
        max_code_chars: int = 1200,
    ) -> List[QueryResult]:
        q.validate()
        structured_dispatch = {
            "symbol_exact": lambda: self.store.symbol_exact(q.text, q.top_k),
            "def_of": lambda: self.def_of(q.symbol_id, q.top_k),
            "refs_of": lambda: self.refs_of(q.symbol_id, q.top_k),
            "callers_of": lambda: self.callers_of(q.symbol_id, q.top_k),
            "callees_of": lambda: self.callees_of(q.symbol_id, q.top_k),
        }
        if q.structured_op in structured_dispatch:
            return self._attach_code(
                self._rerank_with_test_depref(q, structured_dispatch[q.structured_op]()),
                include_code,
                max_code_chars,
            )
        version = embedding_version or self.default_embedding_version

        def semantic_results() -> List[QueryResult]:
            return [
                QueryResult(
                    result_id=chunk_id,
                    result_type="chunk",
                    score=score,
                    explain={"semantic": score},
                    payload=self.store.fetch_chunk_metadata(chunk_id, include_content=False),
                )
                for chunk_id, score in self.embedding_pipeline.semantic_search(q.text, version, q.top_k)
            ]

        if q.mode == "structure":
            structured = self.store.symbol_exact(q.text, q.top_k)
            return self._attach_code(
                self._rerank_with_test_depref(q, structured[: q.top_k]),
                include_code,
                max_code_chars,
            )
        if q.mode == "semantic":
            semantic = semantic_results()
            return self._attach_code(
                self._rerank_with_test_depref(q, semantic[: q.top_k]),
                include_code,
                max_code_chars,
            )

        structured = self.store.symbol_exact(q.text, q.top_k)
        keyword = self.store.keyword_search(q.text, q.top_k)
        semantic = semantic_results()

        fused = (
            self._rrf_fusion(structured, keyword, semantic)
            if q.blend_strategy == "rrf"
            else self._linear_fusion(structured, keyword, semantic)
        )
        reranked = self._rerank_with_test_depref(q, fused)
        return self._attach_code(reranked[: q.top_k], include_code, max_code_chars)
