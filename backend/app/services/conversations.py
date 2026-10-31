"""Conversations and short-term memory: bounded history + deterministic extractive summary of older turns."""
from __future__ import annotations

from typing import Any

from bson import ObjectId

from app.core.config import Settings
from app.core.errors import NotFoundError
from app.core.security import utcnow
from app.guardrails.retrieval import validate_object_id

SUMMARY_MAX_CHARS = 900


def conv_out(c: dict) -> dict:
    return {"id": str(c["_id"]), "title": c.get("title", ""), "topic": c.get("topic"),
            "message_count": c.get("message_count", 0), "document_ids": c.get("document_ids") or [],
            "created_at": c["created_at"], "updated_at": c["updated_at"]}


def msg_out(m: dict) -> dict:
    meta = m.get("meta") or {}
    return {"id": str(m["_id"]), "role": m["role"], "content": m["content"], "created_at": m["created_at"],
            "meta": meta}


def build_summary(older: list[dict]) -> str | None:
    """Extractive, LLM-free summary of turns that fell out of the history window (no extra token cost,
    no hallucination risk). Keeps what the student asked about."""
    asks = [m["content"].strip().replace("\n", " ")[:120] for m in older if m["role"] == "user"]
    if not asks:
        return None
    text = "Earlier the student asked about: " + "; ".join(asks)
    return text[:SUMMARY_MAX_CHARS]


class ConversationService:
    def __init__(self, db: Any, settings: Settings):
        self.db, self.s = db, settings

    async def owned(self, user_id: str, conv_id: str) -> dict:
        validate_object_id(conv_id, "conversation id")
        c = await self.db.conversations.find_one({"_id": ObjectId(conv_id), "user_id": user_id})
        if not c:
            raise NotFoundError("Conversation not found")
        return c

    async def owned_ready_documents(self, user_id: str, requested: list[str] | None) -> list[str]:
        """Server-side authorization: keep only ready documents that belong to this user (client ids are never trusted)."""
        if not requested:
            return []
        ids = [ObjectId(validate_object_id(d, "document id")) for d in dict.fromkeys(requested)]
        rows = await self.db.documents.find({"user_id": user_id, "status": "ready", "_id": {"$in": ids}}).to_list(100)
        return [str(r["_id"]) for r in rows]

    async def set_scope(self, user_id: str, conv: dict, document_ids: list[str]) -> None:
        """Persist the document scope of a thread so it survives reloads and follow-up turns."""
        if sorted(conv.get("document_ids") or []) == sorted(document_ids):
            return
        conv["document_ids"] = document_ids
        await self.db.conversations.update_one({"_id": conv["_id"], "user_id": user_id},
                                               {"$set": {"document_ids": document_ids}})

    async def get_or_create(self, user_id: str, conv_id: str | None, first_message: str, topic: str | None,
                            document_ids: list[str] | None = None) -> dict:
        if conv_id:
            return await self.owned(user_id, conv_id)
        doc = {"user_id": user_id, "title": first_message.strip().replace("\n", " ")[:60], "topic": topic,
               "document_ids": document_ids or [],
               "message_count": 0, "created_at": utcnow(), "updated_at": utcnow()}
        res = await self.db.conversations.insert_one(doc)
        doc["_id"] = res.inserted_id
        return doc

    async def context(self, user_id: str, conv: dict) -> tuple[list[dict], str | None]:
        """Returns (recent history window, summary of older turns)."""
        rows = await self.db.messages.find({"conversation_id": str(conv["_id"]), "user_id": user_id}) \
            .sort("created_at", 1).to_list(500)
        limit = self.s.conversation_history_limit
        recent, older = rows[-limit:], rows[:-limit]
        history = [{"role": m["role"], "content": m["content"]} for m in recent]
        return history, build_summary(older)

    async def add_messages(self, user_id: str, conv: dict, user_text: str, assistant_text: str, meta: dict) -> dict:
        cid = str(conv["_id"])
        now = utcnow()
        await self.db.messages.insert_one({"conversation_id": cid, "user_id": user_id, "role": "user",
                                           "content": user_text, "created_at": now})
        res = await self.db.messages.insert_one({"conversation_id": cid, "user_id": user_id, "role": "assistant",
                                                 "content": assistant_text, "meta": meta, "created_at": utcnow()})
        await self.db.conversations.update_one(
            {"_id": conv["_id"], "user_id": user_id},
            {"$inc": {"message_count": 2}, "$set": {"updated_at": utcnow()}})
        return {"id": str(res.inserted_id)}

    async def list(self, user_id: str, limit: int, skip: int) -> dict:
        total = await self.db.conversations.count_documents({"user_id": user_id})
        rows = await self.db.conversations.find({"user_id": user_id}).sort("updated_at", -1).skip(skip).to_list(limit)
        return {"items": [conv_out(c) for c in rows], "total": total}

    async def detail(self, user_id: str, conv_id: str) -> dict:
        c = await self.owned(user_id, conv_id)
        msgs = await self.db.messages.find({"conversation_id": conv_id, "user_id": user_id}) \
            .sort("created_at", 1).to_list(500)
        return {**conv_out(c), "messages": [msg_out(m) for m in msgs]}

    async def delete(self, user_id: str, conv_id: str) -> None:
        c = await self.owned(user_id, conv_id)
        await self.db.messages.delete_many({"conversation_id": conv_id, "user_id": user_id})
        await self.db.conversations.delete_one({"_id": c["_id"], "user_id": user_id})
