
"""Memory-conscious MiniLM embeddings and persistent FAISS storage for PDF ingestion."""

import asyncio
import json
import logging
import os
import shutil
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import faiss
import numpy as np
from sentence_transformers import SentenceTransformer
from huggingface_hub import snapshot_download
from huggingface_hub.errors import LocalEntryNotFoundError

from config.db import chunk_collection

logger = logging.getLogger(__name__)

UPLOAD_DIR = Path(__file__).resolve().parents[1] / "upload_docs"
FAISS_DIR = Path(__file__).resolve().parents[1] / "faiss_store"
BACKUP_DIR = Path(__file__).resolve().parents[1] / "faiss_store_backup"

INDEX_PATH = FAISS_DIR / "index.faiss"
MAPPING_PATH = FAISS_DIR / "mapping.json"

MODEL_NAME = os.getenv(
    "BGE_MODEL_NAME",
    "sentence-transformers/all-MiniLM-L6-v2",
)

# Keep embedding batches small for low-memory hosting.
EMBED_BATCH_SIZE = max(
    1, min(32, int(os.getenv("EMBED_BATCH_SIZE", "8")))
)
MAX_PDF_BYTES = 15 * 1024 * 1024

_STORE_LOCK = threading.RLock()
_MODEL_LOCK = threading.Lock()
_EMBEDDING_MODEL = None


def _get_embedding_model():
    """Load the embedding model once per process."""
    global _EMBEDDING_MODEL

    if _EMBEDDING_MODEL is None:
        with _MODEL_LOCK:
            if _EMBEDDING_MODEL is None:
                try:
                    model_path = snapshot_download(
                        MODEL_NAME,
                        local_files_only=True,
                    )
                    local_only = True
                except LocalEntryNotFoundError:
                    model_path = MODEL_NAME
                    local_only = False

                _EMBEDDING_MODEL = SentenceTransformer(
                    model_path,
                    local_files_only=local_only,
                    device="cpu",
                )

    return _EMBEDDING_MODEL


def _empty_mapping():
    return {"next_id": 1, "ids": {}}


def _load_mapping():
    if not MAPPING_PATH.exists():
        return _empty_mapping()

    with MAPPING_PATH.open("r", encoding="utf-8") as file:
        mapping = json.load(file)

    if not isinstance(mapping.get("ids"), dict):
        raise RuntimeError("FAISS mapping file is invalid")

    return mapping


def _backup_store():
    """Preserve the existing index and mapping before replacing them."""
    if not INDEX_PATH.exists() and not MAPPING_PATH.exists():
        return

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    for source in (INDEX_PATH, MAPPING_PATH):
        if source.exists():
            destination = BACKUP_DIR / source.name
            if not destination.exists():
                shutil.copy2(source, destination)


def _save_store(index, mapping):
    """Save index and mapping through temporary files."""
    FAISS_DIR.mkdir(parents=True, exist_ok=True)

    index_tmp = INDEX_PATH.with_suffix(".faiss.tmp")
    mapping_tmp = MAPPING_PATH.with_suffix(".json.tmp")

    try:
        faiss.write_index(index, str(index_tmp))

        with mapping_tmp.open("w", encoding="utf-8") as file:
            json.dump(mapping, file)

        # Preserve a recoverable copy before replacing the current store.
        _backup_store()

        os.replace(index_tmp, INDEX_PATH)
        os.replace(mapping_tmp, MAPPING_PATH)
    finally:
        index_tmp.unlink(missing_ok=True)
        mapping_tmp.unlink(missing_ok=True)


def _new_index(dimension):
    return faiss.IndexIDMap2(faiss.IndexFlatIP(dimension))


def _load_index():
    """Load the existing index, refusing silent dimension mismatches."""
    FAISS_DIR.mkdir(parents=True, exist_ok=True)

    if not INDEX_PATH.exists():
        if MAPPING_PATH.exists():
            raise RuntimeError(
                "FAISS mapping exists without its index. "
                "Restore the backup before continuing."
            )
        return _new_index(
            _get_embedding_model().get_sentence_embedding_dimension()
        )

    if not MAPPING_PATH.exists():
        raise RuntimeError(
            "FAISS index exists without its mapping. "
            "Restore the backup before continuing."
        )

    index = faiss.read_index(str(INDEX_PATH))
    dimension = _get_embedding_model().get_sentence_embedding_dimension()

    if index.d != dimension:
        raise RuntimeError(
            f"FAISS dimension mismatch: existing index has {index.d} "
            f"dimensions, but model {MODEL_NAME!r} produces {dimension}. "
            "A safe re-embedding migration is required before upload/search."
        )

    if not isinstance(index, faiss.IndexIDMap):
        raise RuntimeError("Unexpected FAISS index format")

    return index


def _embed_texts(model, texts):
    """Encode text in bounded batches instead of embedding all at once."""
    if not texts:
        return np.empty(
            (0, model.get_sentence_embedding_dimension()),
            dtype=np.float32,
        )

    batches = []

    for start in range(0, len(texts), EMBED_BATCH_SIZE):
        batch = texts[start:start + EMBED_BATCH_SIZE]

        vectors = model.encode(
            batch,
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
            batch_size=EMBED_BATCH_SIZE,
        )

        vectors = np.asarray(vectors, dtype=np.float32)

        if vectors.ndim != 2 or vectors.shape[0] != len(batch):
            raise ValueError("Embedding model returned an invalid shape")

        if not np.isfinite(vectors).all():
            raise ValueError("Embedding model returned invalid values")

        batches.append(vectors)

    return np.concatenate(batches, axis=0)


def add_vectors(chunk_ids, vectors):
    """Add normalized vectors using MongoDB chunk IDs as external IDs."""
    if not chunk_ids:
        return

    if len(set(chunk_ids)) != len(chunk_ids):
        raise ValueError("Duplicate chunk IDs were supplied")

    matrix = np.asarray(vectors, dtype=np.float32).copy()

    if matrix.ndim != 2 or matrix.shape[0] != len(chunk_ids):
        raise ValueError("Vector count does not match chunk IDs")

    if not np.isfinite(matrix).all():
        raise ValueError("Embeddings contain invalid numeric values")

    dimension = _get_embedding_model().get_sentence_embedding_dimension()

    if matrix.shape[1] != dimension:
        raise ValueError(
            f"Embedding dimension {matrix.shape[1]} does not match "
            f"configured model dimension {dimension}"
        )

    faiss.normalize_L2(matrix)

    with _STORE_LOCK:
        index = _load_index()
        mapping = _load_mapping()
        existing_ids = mapping["ids"]

        if any(chunk_id in existing_ids for chunk_id in chunk_ids):
            raise ValueError("One or more chunk IDs already exist in FAISS")

        numeric_ids = []
        next_id = int(mapping.get("next_id", 1))

        for chunk_id in chunk_ids:
            numeric_ids.append(next_id)
            existing_ids[chunk_id] = next_id
            next_id += 1

        numeric_array = np.asarray(numeric_ids, dtype=np.int64)
        index.add_with_ids(matrix, numeric_array)
        mapping["next_id"] = next_id

        try:
            _save_store(index, mapping)
        except Exception:
            index.remove_ids(numeric_array)

            for chunk_id in chunk_ids:
                existing_ids.pop(chunk_id, None)

            raise


def delete_vectors(chunk_ids):
    """Delete FAISS vectors and their external-ID mappings."""
    if not chunk_ids:
        return

    requested = set(chunk_ids)

    with _STORE_LOCK:
        if not INDEX_PATH.exists() and not MAPPING_PATH.exists():
            return

        index = _load_index()
        mapping = _load_mapping()

        numeric_ids = [
            mapping["ids"][chunk_id]
            for chunk_id in requested
            if chunk_id in mapping["ids"]
        ]

        if not numeric_ids:
            return

        index.remove_ids(np.asarray(numeric_ids, dtype=np.int64))

        for chunk_id in requested:
            mapping["ids"].pop(chunk_id, None)

        _save_store(index, mapping)


def search_vectors(query, candidate_count=50):
    """Return chunk IDs ranked by cosine similarity."""
    if not query or not query.strip():
        return []

    model = _get_embedding_model()

    # MiniLM does not require the old BGE-specific query prefix.
    vector = model.encode(
        [query.strip()],
        normalize_embeddings=True,
        convert_to_numpy=True,
        show_progress_bar=False,
    )
    vector = np.asarray(vector, dtype=np.float32)

    with _STORE_LOCK:
        if not INDEX_PATH.exists() or not MAPPING_PATH.exists():
            return []

        index = _load_index()
        mapping = _load_mapping()

        if index.ntotal == 0:
            return []

        if vector.shape[1] != index.d:
            raise RuntimeError(
                "Query embedding dimension does not match FAISS index"
            )

        count = min(max(1, int(candidate_count)), index.ntotal)
        scores, ids = index.search(vector, count)

        reverse_mapping = {
            int(numeric_id): chunk_id
            for chunk_id, numeric_id in mapping["ids"].items()
        }

        results = []

        for score, numeric_id in zip(scores[0], ids[0]):
            numeric_id = int(numeric_id)

            if numeric_id in reverse_mapping:
                results.append({
                    "id": reverse_mapping[numeric_id],
                    "score": float(score),
                })

        return results


async def load_vectorstore(
    uploaded_files,
    role: str,
    doc_id: str,
    grade: int,
    owner_id: str | None = None,
):
    """Parse PDFs, batch-embed chunks, and persist metadata and vectors."""
    from langchain_community.document_loaders import PyPDFLoader
    from langchain_text_splitters import RecursiveCharacterTextSplitter

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

    for uploaded_file in uploaded_files:
        original_name = Path(
            uploaded_file.filename or "source.pdf"
        ).name

        save_path = UPLOAD_DIR / f"{doc_id}-{original_name}"
        vector_ids = []
        mongo_written = False

        try:
            await uploaded_file.seek(0)
            content = await uploaded_file.read()

            if not content:
                raise ValueError("Uploaded PDF is empty")

            if len(content) > MAX_PDF_BYTES:
                raise ValueError("PDF must be 15 MB or smaller")

            if b"%PDF-" not in content[:1024]:
                raise ValueError("Uploaded file is not a valid PDF")

            save_path.write_bytes(content)

            try:
                documents = await asyncio.to_thread(
                    PyPDFLoader(str(save_path)).load
                )
            except Exception as exc:
                raise ValueError("The PDF could not be parsed") from exc

            chunk_size = max(
                300,
                min(1500, int(os.getenv("RAG_CHUNK_SIZE", "700"))),
            )
            chunk_overlap = max(
                0,
                min(300, int(os.getenv("RAG_CHUNK_OVERLAP", "80"))),
            )

            splitter = RecursiveCharacterTextSplitter(
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap,
            )

            chunks = [
                chunk
                for chunk in splitter.split_documents(documents)
                if chunk.page_content.strip()
            ]

            if not chunks:
                raise ValueError(
                    "The PDF contains no extractable text. "
                    "Scanned PDFs require OCR, which is not configured."
                )

            model = _get_embedding_model()
            texts = [chunk.page_content for chunk in chunks]

            # Encode small batches to reduce peak working memory.
            vectors = await asyncio.to_thread(
                _embed_texts, model, texts
            )

            vector_ids = [
                f"{doc_id}-{number}"
                for number in range(len(chunks))
            ]

            uploaded_at = datetime.now(timezone.utc)
            expires_at = uploaded_at + timedelta(days=30)

            chunk_docs = []

            for number, chunk in enumerate(chunks):
                metadata = {
                    "doc_id": doc_id,
                    "page": int(chunk.metadata.get("page", 0)),
                    "source": original_name,
                    "grade": grade,
                    "role": role,
                    "uploaded_at": uploaded_at,
                    "expires_at": expires_at,
                    "section": (
                        chunk.metadata.get("title")
                        or chunk.metadata.get("section")
                        or ""
                    ),
                }

                if owner_id:
                    metadata["owner_id"] = owner_id

                chunk_docs.append({
                    "chunk_id": vector_ids[number],
                    **metadata,
                    "text": chunk.page_content,
                })

            # Check existing FAISS dimensions before writing new Mongo chunks.
            # This deliberately fails safely when a migration is required.
            await asyncio.to_thread(_load_index)

            await asyncio.to_thread(
                chunk_collection.insert_many,
                chunk_docs,
            )
            mongo_written = True

            await asyncio.to_thread(
                add_vectors, vector_ids, vectors
            )

        except Exception:
            logger.exception(
                "PDF ingestion failed for document %s",
                doc_id,
            )

            if vector_ids:
                try:
                    await asyncio.to_thread(delete_vectors, vector_ids)
                except Exception:
                    logger.exception(
                        "Failed to roll back FAISS vectors for %s",
                        doc_id,
                    )

            if mongo_written:
                try:
                    await asyncio.to_thread(
                        chunk_collection.delete_many,
                        {
                            "doc_id": doc_id,
                            "owner_id": owner_id,
                        },
                    )
                except Exception:
                    logger.exception(
                        "Failed to roll back MongoDB chunks for %s",
                        doc_id,
                    )

            save_path.unlink(missing_ok=True)
            raise


def cleanup_expired_documents():
    """Delete expired document vectors, PDFs and MongoDB chunks."""
    now = datetime.now(timezone.utc)

    expired_chunks = list(
        chunk_collection.find(
            {"expires_at": {"$lte": now}},
            {
                "_id": 0,
                "doc_id": 1,
                "chunk_id": 1,
            },
        )
    )

    documents = {}

    for chunk in expired_chunks:
        doc_id = chunk.get("doc_id")

        if not doc_id:
            continue

        item = documents.setdefault(
            doc_id,
            {"chunk_ids": []},
        )

        if chunk.get("chunk_id"):
            item["chunk_ids"].append(chunk["chunk_id"])

    deleted_documents = 0
    deleted_chunks = 0

    for doc_id, item in documents.items():
        # Do not delete MongoDB records if vector deletion fails.
        delete_vectors(item["chunk_ids"])

        for pdf_file in UPLOAD_DIR.glob(doc_id + "-*"):
            pdf_file.unlink(missing_ok=True)

        result = chunk_collection.delete_many({
            "doc_id": doc_id,
            "expires_at": {"$lte": now},
        })

        if result.deleted_count:
            deleted_documents += 1
            deleted_chunks += result.deleted_count

    return {
        "documents_deleted": deleted_documents,
        "chunks_deleted": deleted_chunks,
    }
