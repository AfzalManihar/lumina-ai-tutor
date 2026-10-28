
import asyncio
import uuid
from io import BytesIO

from fastapi import APIRouter, HTTPException, UploadFile, File, Form, Depends
from starlette.datastructures import UploadFile as StarletteUploadFile

from auth.route import authenticate
from config.db import chunk_collection

router = APIRouter()

MAX_PDF_SIZE = 15 * 1024 * 1024


def _validate_doc_id(doc_id: str) -> str:
    try:
        return str(uuid.UUID(doc_id))
    except (ValueError, TypeError, AttributeError) as exc:
        raise HTTPException(
            status_code=400,
            detail="Invalid document id",
        ) from exc


def _get_owned_chunks(doc_id: str, user: dict):
    return list(
        chunk_collection.find(
            {
                "doc_id": doc_id,
                "owner_id": user["user_id"],
            },
            {
                "_id": 0,
                "chunk_id": 1,
                "role": 1,
                "grade": 1,
                "source": 1,
                "page": 1,
            },
        )
    )


async def _delete_owned_document_chunks(doc_id: str, user: dict):
    query = {
        "doc_id": doc_id,
        "owner_id": user["user_id"],
    }

    chunks = await asyncio.to_thread(
        lambda: list(
            chunk_collection.find(
                query,
                {
                    "_id": 0,
                    "chunk_id": 1,
                    "role": 1,
                    "grade": 1,
                    "source": 1,
                    "page": 1,
                },
            )
        )
    )

    if not chunks:
        raise HTTPException(
            status_code=404,
            detail="Owned document not found",
        )

    chunk_ids = [
        item["chunk_id"]
        for item in chunks
        if item.get("chunk_id")
    ]

    from .vectorstore import delete_vectors

    # Delete vectors first. If this fails, preserve MongoDB metadata.
    await asyncio.to_thread(delete_vectors, chunk_ids)

    # If this fails, metadata remains available for recovery.
    await asyncio.to_thread(
        chunk_collection.delete_many,
        query,
    )

    return chunks


@router.post("/upload_docs")
async def upload_docs(
    file: UploadFile = File(...),
    grade: int = Form(...),
    user=Depends(authenticate),
):
    if user.get("role") != "Teacher":
        raise HTTPException(
            status_code=403,
            detail="Only teachers can publish shared study materials",
        )

    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(
            status_code=400,
            detail="Only PDF files are supported",
        )

    if not 1 <= grade <= 12:
        raise HTTPException(
            status_code=422,
            detail="Grade must be between 1 and 12",
        )

    content = await file.read(MAX_PDF_SIZE + 1)

    if len(content) > MAX_PDF_SIZE:
        raise HTTPException(
            status_code=413,
            detail="PDF must be 15 MB or smaller",
        )

    if not content:
        raise HTTPException(
            status_code=400,
            detail="Uploaded PDF is empty",
        )

    if b"%PDF-" not in content[:1024]:
        raise HTTPException(
            status_code=400,
            detail="The uploaded file is not a readable PDF",
        )

    await file.seek(0)
    doc_id = str(uuid.uuid4())

    try:
        from .vectorstore import load_vectorstore

        await load_vectorstore(
            uploaded_files=[file],
            role="Public",
            doc_id=doc_id,
            grade=grade,
            owner_id=user["user_id"],
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail="Failed to process and index the PDF",
        ) from exc

    return {
        "message": f"{file.filename} uploaded and indexed successfully",
        "doc_id": doc_id,
        "grade": grade,
        "access": "Public",
    }


@router.get("/documents/mine")
async def list_my_documents(user=Depends(authenticate)):
    try:
        chunks = await asyncio.to_thread(
            lambda: list(
                chunk_collection.find(
                    {"owner_id": user["user_id"]},
                    {
                        "_id": 0,
                        "doc_id": 1,
                        "source": 1,
                        "page": 1,
                    },
                ).sort("doc_id", 1)
            )
        )
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail="Study material storage is unavailable",
        ) from exc

    documents = {}

    for chunk in chunks:
        doc_id = chunk.get("doc_id")
        if not doc_id:
            continue

        item = documents.setdefault(
            doc_id,
            {
                "source_id": doc_id,
                "filename": chunk.get("source", "Study material"),
                "page_numbers": set(),
            },
        )

        item["page_numbers"].add(int(chunk.get("page", 0)) + 1)

    return {
        "documents": [
            {
                "source_id": item["source_id"],
                "filename": item["filename"],
                "pages": len(item["page_numbers"]),
            }
            for item in documents.values()
        ]
    }


@router.delete("/documents/{doc_id}")
async def delete_my_document(
    doc_id: str,
    user=Depends(authenticate),
):
    doc_id = _validate_doc_id(doc_id)

    chunks = await _delete_owned_document_chunks(doc_id, user)

    from .vectorstore import UPLOAD_DIR

    try:
        for local_file in UPLOAD_DIR.glob(doc_id + "-*"):
            await asyncio.to_thread(local_file.unlink, missing_ok=True)
    except Exception as exc:
        # Metadata and vectors have already been deleted.
        # Report the remaining file-cleanup failure explicitly.
        raise HTTPException(
            status_code=503,
            detail="Document data was deleted, but PDF file cleanup failed",
        ) from exc

    return {
        "deleted": True,
        "doc_id": doc_id,
        "chunks_deleted": len(chunks),
    }


@router.post("/documents/{doc_id}/reindex")
async def reindex_my_document(
    doc_id: str,
    user=Depends(authenticate),
):
    doc_id = _validate_doc_id(doc_id)

    from .vectorstore import UPLOAD_DIR, load_vectorstore

    # Verify ownership before reading or changing the document.
    chunks = await asyncio.to_thread(_get_owned_chunks, doc_id, user)

    if not chunks:
        raise HTTPException(
            status_code=404,
            detail="Owned document not found",
        )

    local_files = list(UPLOAD_DIR.glob(doc_id + "-*"))

    if not local_files:
        raise HTTPException(
            status_code=404,
            detail="Original PDF is unavailable for re-indexing",
        )

    first = chunks[0]

    try:
        content = await asyncio.to_thread(local_files[0].read_bytes)

        if not content or b"%PDF-" not in content[:1024]:
            raise ValueError("Stored PDF is empty or invalid")

        if len(content) > MAX_PDF_SIZE:
            raise ValueError("Stored PDF exceeds the 15 MB limit")

        # Reuse the stored PDF without deleting the original file.
        upload = StarletteUploadFile(
            filename=first.get("source") or local_files[0].name,
            file=BytesIO(content),
        )

        # NOTE:
        # load_vectorstore must support safe replacement/reindexing.
        # Do not delete old chunks first: a failed indexing operation
        # could leave the document unavailable.
        #
        # This call is safe only if load_vectorstore handles existing
        # doc_id/chunk_id collisions or replacement transactionally.
        await load_vectorstore(
            uploaded_files=[upload],
            role=first.get("role", "Private"),
            doc_id=doc_id,
            grade=int(first.get("grade", 8)),
            owner_id=user["user_id"],
        )

    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail="Document re-indexing failed; the original PDF was retained",
        ) from exc

    return {
        "reindexed": True,
        "doc_id": doc_id,
    }
