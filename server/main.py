
"""FastAPI application and same-origin Lumina frontend."""

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from auth.route import router as auth_router
from chat.route import router as chat_router
from config.db import initialize_indexes
from config.settings import ConfigurationError
from courses.routes import router as course_router
from docs.route import router as doc_router
from billing import router as billing_router


logger = logging.getLogger(__name__)

CLEANUP_INTERVAL_SECONDS = 60 * 60


async def _cleanup_worker():
    """Run document expiry cleanup at startup and every hour."""
    from docs.vectorstore import cleanup_expired_documents

    while True:
        try:
            result = await asyncio.to_thread(
                cleanup_expired_documents
            )

            logger.info(
                "Expired document cleanup completed: %s",
                result,
            )

        except asyncio.CancelledError:
            raise

        except Exception:
            logger.exception(
                "Expired document cleanup failed; will retry later"
            )

        await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    cleanup_task = None

    if os.getenv("MONGO_URI", "").strip():
        try:
            initialize_indexes()
        except Exception as exc:
            raise RuntimeError(
                "MongoDB is configured but unavailable; "
                "verify network access and database credentials"
            ) from exc

        cleanup_task = asyncio.create_task(_cleanup_worker())
        logger.info("Document expiry cleanup worker started")
    else:
        logger.warning(
            "MONGO_URI is not configured; cleanup worker was not started"
        )

    try:
        yield
    finally:
        if cleanup_task is not None:
            cleanup_task.cancel()

            try:
                await cleanup_task
            except asyncio.CancelledError:
                pass

            logger.info("Document expiry cleanup worker stopped")


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

app = FastAPI(
    title="Lumina AI Tutor API",
    version="1.1.0",
    lifespan=lifespan,
)

origins = [
    origin.strip()
    for origin in os.getenv(
        "CORS_ORIGINS",
        "http://localhost:8000,http://127.0.0.1:8000",
    ).split(",")
    if origin.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

app.include_router(auth_router)
app.include_router(doc_router)
app.include_router(chat_router)
app.include_router(course_router)
app.include_router(billing_router)


@app.exception_handler(ConfigurationError)
async def configuration_error_handler(
    request: Request,
    exc: ConfigurationError,
):
    return JSONResponse(
        status_code=503,
        content={"detail": str(exc)},
    )


@app.get("/health")
def health():
    return {
        "status": "ok",
        "database_configured": bool(
            os.getenv("MONGO_URI", "").strip()
        ),
        "tutor_configured": bool(
            os.getenv("GROQ_API_KEY", "").strip()
        ),
        "document_rag_configured": bool(
            os.getenv("MONGO_URI", "").strip()
            and os.getenv("GROQ_API_KEY", "").strip()
        ),
        "embedding_provider": "local_sentence_transformers",
        "vector_store": "local_faiss",
    }


frontend_dir = (
    Path(__file__).resolve().parents[1]
    / "client"
    / "lumina"
)

if frontend_dir.is_dir():
    app.mount(
        "/",
        StaticFiles(directory=frontend_dir, html=True),
        name="lumina",
    )
