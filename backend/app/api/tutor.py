"""Tutor chat: one request = one bounded, metered supervisor workflow."""
from __future__ import annotations

from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Depends, Query, Request

from app.api.deps import current_user, db_dep, settings_dep, uid
from app.core.config import Settings
from app.guardrails.input import check_user_input
from app.observability.tracing import redact
from app.orchestration.supervisor import Supervisor
from app.schemas.learning import ChatIn
from app.services.container import get_lazy_retriever, get_llm_for
from app.services.conversations import ConversationService
from app.services.metering import metered

router = APIRouter(prefix="/tutor", tags=["tutor"])


@router.post("/chat")
async def chat(body: ChatIn, request: Request, user: dict = Depends(current_user), db: Any = Depends(db_dep),
               s: Settings = Depends(settings_dep)):
    user_id = uid(user)
    check_user_input(body.message, s.max_input_chars)  # reject malformed input before any metering/LLM spend
    convs = ConversationService(db, s)
    topic = body.topic or (user.get("settings") or {}).get("learning_topic") or None
    # Document scope: an explicit list from the client is re-checked against ownership; otherwise the thread's
    # saved scope is reused, so follow-ups ("explain that again") stay tied to the same PDF(s).
    requested = await convs.owned_ready_documents(user_id, body.document_ids) if body.document_ids is not None else None
    conv = await convs.get_or_create(user_id, body.conversation_id, body.message, topic, requested)
    if requested is not None:
        await convs.set_scope(user_id, conv, requested)
    scope = requested if requested is not None else list(conv.get("document_ids") or [])
    history, summary = await convs.context(user_id, conv)
    has_docs = await db.documents.count_documents({"user_id": user_id, "status": "ready"}) > 0
    retriever = get_lazy_retriever(request)

    async with metered(db, s, get_llm_for(request), user_id, "chat", request.state.request_id) as (llm, trace):
        sup = Supervisor(db, s, llm, trace, retriever, user)
        result = await sup.run(body.message, history=history, summary=summary, document_ids=scope if (requested is not None or scope) else None,
                               use_documents=body.use_documents or bool(scope) or bool(body.document_ids), topic=topic, has_documents=has_docs)

    answer = redact(result.answer, s.secret_values)
    meta = {"intent": result.intent, "grounding_status": result.grounding_status, "citations": result.citations,
            "limitations": result.limitations, "flags": result.flags, "follow_ups": result.follow_ups,
            "quiz_id": result.quiz_id, "lesson_id": result.lesson_id, "status": result.status,
            "steps": result.steps, "stats": result.stats}
    saved = await convs.add_messages(user_id, conv, body.message, answer, meta)
    out = asdict(result)
    out.update({"answer": answer, "conversation_id": str(conv["_id"]), "message_id": saved["id"]})
    return out


@router.get("/conversations")
async def list_conversations(limit: int = Query(20, ge=1, le=100), skip: int = Query(0, ge=0),
                             user: dict = Depends(current_user), db: Any = Depends(db_dep),
                             s: Settings = Depends(settings_dep)):
    return await ConversationService(db, s).list(uid(user), limit, skip)


@router.get("/conversations/{conv_id}")
async def get_conversation(conv_id: str, user: dict = Depends(current_user), db: Any = Depends(db_dep),
                           s: Settings = Depends(settings_dep)):
    return await ConversationService(db, s).detail(uid(user), conv_id)


@router.delete("/conversations/{conv_id}", status_code=204)
async def delete_conversation(conv_id: str, user: dict = Depends(current_user), db: Any = Depends(db_dep),
                              s: Settings = Depends(settings_dep)):
    await ConversationService(db, s).delete(uid(user), conv_id)
