"""Privacy-safe structured local request tracing."""
import datetime as dt
import json
import logging
import uuid
from pydantic import BaseModel, Field

logger = logging.getLogger("lumina.trace")
class TraceEvent(BaseModel):
    trace_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    user_ref: str
    workflow: str
    steps: list[str] = Field(default_factory=list, max_length=10)
    retrieval_count: int = 0
    retries: int = 0
    latency_ms: int = 0
    token_count: int | None = None
    token_source: str | None = None
    error_category: str | None = None
    verification_status: str | None = None
    created_at: dt.datetime = Field(default_factory=lambda: dt.datetime.now(dt.timezone.utc))

def emit_trace(event: TraceEvent) -> dict:
    payload = event.model_dump(mode="json")
    logger.info(json.dumps(payload, separators=(",", ":")))
    return payload
