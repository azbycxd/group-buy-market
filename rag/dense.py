from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any, Iterable, Sequence

import numpy as np

from rag.bm25 import SearchResult
from rag.chunking import PROJECT_ROOT, RuleChunk


MODEL_NAME = "BAAI/bge-small-zh-v1.5"
QUERY_PREFIX = "为这个句子生成表示以用于检索相关文章："
DEFAULT_CACHE_DIR = PROJECT_ROOT / "data" / "rag_cache"


def _normalized(vectors: Any) -> np.ndarray:
    array = np.asarray(vectors, dtype=np.float32)
    if array.ndim == 1:
        array = array.reshape(1, -1)
    if array.ndim != 2 or array.shape[1] == 0:
        raise ValueError("Dense encoder 返回的向量形状非法")
    norms = np.linalg.norm(array, axis=1, keepdims=True)
    if np.any(norms == 0) or not np.all(np.isfinite(array)):
        raise ValueError("Dense encoder 返回了零向量或非有限值")
    return array / norms


class DenseIndex:
    def __init__(
        self,
        chunks: Sequence[RuleChunk],
        *,
        model_name: str = MODEL_NAME,
        cache_dir: str | Path | None = None,
        batch_size: int = 32,
        model: Any | None = None,
    ) -> None:
        if not chunks:
            raise ValueError("Dense index 至少需要一个规则 chunk")
        if batch_size < 1:
            raise ValueError("batch_size 必须大于 0")
        self.chunks = list(chunks)
        self.model_name = model_name
        self.batch_size = batch_size
        configured_cache = os.getenv("RAG_DENSE_CACHE_DIR")
        self.cache_dir = Path(
            cache_dir or configured_cache or DEFAULT_CACHE_DIR
        )
        model_namespace = hashlib.sha256(
            model_name.encode("utf-8")
        ).hexdigest()[:16]
        self._model_cache_dir = self.cache_dir / model_namespace
        self._model_cache_dir.mkdir(parents=True, exist_ok=True)
        self._model = model or self._load_model(model_name)
        self._document_embeddings = self._load_document_embeddings()

    @staticmethod
    def _load_model(model_name: str) -> Any:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as error:  # pragma: no cover - dependency guard
            raise RuntimeError(
                "Dense retrieval 需要 sentence-transformers"
            ) from error
        return SentenceTransformer(model_name)

    def _content_cache_path(self, chunk: RuleChunk) -> Path:
        content_hash = hashlib.sha256(
            chunk.text.encode("utf-8")
        ).hexdigest()
        return self._model_cache_dir / f"{content_hash}.npy"

    def _encode(self, texts: list[str]) -> np.ndarray:
        encoded = self._model.encode(
            texts,
            batch_size=self.batch_size,
            convert_to_numpy=True,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        return _normalized(encoded)

    def _load_cached_vector(self, path: Path) -> np.ndarray | None:
        if not path.exists():
            return None
        try:
            vector = np.load(path, allow_pickle=False)
            return _normalized(vector)[0]
        except (OSError, ValueError):
            return None

    @staticmethod
    def _write_cached_vector(path: Path, vector: np.ndarray) -> None:
        temporary = path.with_suffix(f".{os.getpid()}.tmp")
        try:
            with temporary.open("wb") as file:
                np.save(file, vector, allow_pickle=False)
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()

    def _load_document_embeddings(self) -> np.ndarray:
        vectors: list[np.ndarray | None] = []
        missing_indexes: list[int] = []
        missing_texts: list[str] = []
        cache_paths: list[Path] = []

        for index, chunk in enumerate(self.chunks):
            cache_path = self._content_cache_path(chunk)
            cache_paths.append(cache_path)
            vector = self._load_cached_vector(cache_path)
            vectors.append(vector)
            if vector is None:
                missing_indexes.append(index)
                missing_texts.append(chunk.text)

        if missing_texts:
            encoded = self._encode(missing_texts)
            for index, vector in zip(
                missing_indexes, encoded, strict=True
            ):
                vectors[index] = vector
                self._write_cached_vector(cache_paths[index], vector)

        dimensions = {
            int(vector.shape[0])
            for vector in vectors
            if vector is not None
        }
        if len(dimensions) != 1 or any(vector is None for vector in vectors):
            raise ValueError("Dense 文档缓存的向量维度不一致")
        return np.stack(vectors).astype(np.float32, copy=False)  # type: ignore[arg-type]

    def search(self, query: str, *, top_k: int = 5) -> list[SearchResult]:
        if top_k < 1:
            raise ValueError("top_k 必须大于 0")
        query_vector = self._encode([f"{QUERY_PREFIX}{query}"])[0]
        scores = self._document_embeddings @ query_vector
        ranked = sorted(
            zip(scores.tolist(), self.chunks, strict=True),
            key=lambda item: (-item[0], item[1].rule_id),
        )
        return [
            SearchResult(
                rule_id=chunk.rule_id,
                section=chunk.section,
                title=chunk.title,
                content=chunk.content,
                score=float(score),
            )
            for score, chunk in ranked[: min(top_k, len(ranked))]
        ]


def build_index(
    chunks: Iterable[RuleChunk],
    **kwargs: Any,
) -> DenseIndex:
    return DenseIndex(list(chunks), **kwargs)
