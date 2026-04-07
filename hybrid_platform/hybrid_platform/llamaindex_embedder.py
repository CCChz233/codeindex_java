from __future__ import annotations

import importlib
from dataclasses import dataclass
from threading import Lock
from typing import Any, Dict, List, Sequence

from .embedding import BaseEmbedder, EmbeddingProviderError, _classify_embedding_exception


def _load_object(class_path: str) -> object:
    module_name, _, attr = class_path.rpartition(".")
    if not module_name or not attr:
        raise ValueError(f"invalid class path: {class_path}")
    module = importlib.import_module(module_name)
    return getattr(module, attr)


@dataclass
class LlamaIndexEmbedder(BaseEmbedder):
    class_path: str
    init_kwargs: Dict[str, Any]
    query_method: str = "query"
    document_method: str = "text"
    allow_batch_fallback: bool = True
    serialize_calls: bool = False

    def __post_init__(self) -> None:
        try:
            cls_obj = _load_object(self.class_path)
        except Exception as exc:  # pragma: no cover
            raise RuntimeError(
                "无法加载 LlamaIndex embedding 类，请检查 embedding.llama.class_path"
            ) from exc
        self._model = cls_obj(**self.init_kwargs)
        self._lock = Lock()

    def _invoke(self, method_name: str, *args: object) -> object:
        fn = getattr(self._model, method_name, None)
        if fn is None:
            raise EmbeddingProviderError(
                f"LlamaIndex embedding 对象缺少 {method_name} 方法",
                category="unsupported",
            )
        try:
            if self.serialize_calls:
                with self._lock:
                    return fn(*args)
            return fn(*args)
        except Exception as exc:
            raise _classify_embedding_exception(exc) from exc

    def _document_single_method_name(self) -> str:
        if self.document_method == "query":
            return "get_query_embedding"
        return "get_text_embedding"

    def _query_method_name(self) -> str:
        if self.query_method == "text":
            return "get_text_embedding"
        return "get_query_embedding"

    def supports_native_batch(self) -> bool:
        return hasattr(self._model, "get_text_embedding_batch")

    def embed_query(self, text: str) -> List[float]:
        primary = self._query_method_name()
        fallback = "get_text_embedding" if primary == "get_query_embedding" else "get_query_embedding"
        if hasattr(self._model, primary):
            vec = self._invoke(primary, text)
        elif hasattr(self._model, fallback):
            vec = self._invoke(fallback, text)
        else:  # pragma: no cover
            raise EmbeddingProviderError(
                "LlamaIndex embedding 对象缺少 query/text embedding 方法",
                category="unsupported",
            )
        return [float(x) for x in vec]

    def embed_documents(self, texts: Sequence[str]) -> List[List[float]]:
        if hasattr(self._model, "get_text_embedding_batch"):
            vecs = self._invoke("get_text_embedding_batch", list(texts))
            return [[float(x) for x in row] for row in vecs]
        if not self.allow_batch_fallback:
            raise EmbeddingProviderError(
                "LlamaIndex embedding 对象缺少 get_text_embedding_batch，且未允许 batch fallback",
                category="unsupported",
            )
        method_name = self._document_single_method_name()
        fallback = "get_query_embedding" if method_name == "get_text_embedding" else "get_text_embedding"
        if hasattr(self._model, method_name):
            return [[float(x) for x in self._invoke(method_name, t)] for t in texts]
        if hasattr(self._model, fallback):
            return [[float(x) for x in self._invoke(fallback, t)] for t in texts]
        raise EmbeddingProviderError(
            "LlamaIndex embedding 对象缺少文档 embedding 方法",
            category="unsupported",
        )

    def embed(self, text: str) -> List[float]:
        return self.embed_query(text)

    def embed_batch(self, texts: Sequence[str]) -> List[List[float]]:
        return self.embed_documents(texts)
