"""Tool registry (validation, permissions, approval, budgets) and the Groq client (mocked transport)."""
import json

import httpx
import pytest

from app.agents.llm import BudgetedLLM, LLMClient, TokenBudgetExceeded
from app.core.errors import ConfigError, ProviderError, RateLimitError
from app.tools.registry import REGISTRY, STUDENT_PERMISSIONS, ToolContext
from tests.conftest import make_settings, register
from tests.fakes import FakeLLM, smart_responder
from tests.pdfs import PHOTO_PAGES, make_pdf


async def ctx_for(client, db, settings, **kw):
    email = (await register(client))["email"]
    user = await db.users.find_one({"email": email})
    from app.rag.embeddings import HashEmbedder
    from app.rag.retriever import Retriever
    from app.rag.store import MongoVectorStore
    r = Retriever(db, settings, HashEmbedder(), MongoVectorStore(db))
    return ToolContext(db=db, settings=settings, user=user, retriever=r, llm=BudgetedLLM(FakeLLM(smart_responder), 10000), **kw)


async def test_unknown_and_non_allowlisted_tools_denied(client, db, settings):
    ctx = await ctx_for(client, db, settings, allowed_tools={"calculate"})
    for name in ("rm_rf", "shell", "save_preference", "../calculate", None, 5):
        assert (await REGISTRY.execute(name, {}, ctx)).status == "denied"
    assert ctx.tool_calls == 0


async def test_argument_validation(client, db, settings):
    ctx = await ctx_for(client, db, settings)
    bad = [("calculate", {"expression": ""}), ("calculate", {"expression": "1+1", "extra": 1}), ("calculate", "1+1"),
           ("search_documents", {"query": "x"}), ("search_documents", {"query": "valid query", "top_k": 99}),
           ("get_passages", {"document_id": "short"}), ("generate_quiz", {"topic": "ok", "difficulty": "godlike"})]
    for name, args in bad:
        assert (await REGISTRY.execute(name, args, ctx)).status == "invalid_args", (name, args)


async def test_calculate_tool_is_deterministic_and_safe(client, db, settings):
    ctx = await ctx_for(client, db, settings)
    assert (await REGISTRY.execute("calculate", {"expression": "3*(4+5)"}, ctx)).output["result"] == 27
    r = await REGISTRY.execute("calculate", {"expression": "__import__('os').getcwd()"}, ctx)
    assert r.status == "error" and "Unsupported" in r.error


async def test_permission_check_blocks_missing_permission(client, db, settings):
    ctx = await ctx_for(client, db, settings, permissions=frozenset({"compute"}))
    assert (await REGISTRY.execute("read_progress", {}, ctx)).status == "denied"
    assert (await REGISTRY.execute("calculate", {"expression": "1+1"}, ctx)).status == "ok"


async def test_write_tool_needs_explicit_approval(client, db, settings):
    ctx = await ctx_for(client, db, settings)
    r = await REGISTRY.execute("save_preference", {"text": "Prefers short answers"}, ctx)
    assert r.status == "needs_approval" and await db.memories.count_documents({}) == 0
    ctx.approved.add("save_preference")
    r = await REGISTRY.execute("save_preference", {"text": "Prefers short answers"}, ctx)
    assert r.status == "ok" and await db.memories.count_documents({}) == 1
    bad = await REGISTRY.execute("save_preference", {"text": "my password is abc12345"}, ctx)
    assert bad.status == "error" and await db.memories.count_documents({}) == 1


async def test_tool_call_budget(client, db, settings):
    ctx = await ctx_for(client, db, settings, max_tool_calls=2)
    for _ in range(2):
        assert (await REGISTRY.execute("calculate", {"expression": "1+1"}, ctx)).status == "ok"
    assert (await REGISTRY.execute("calculate", {"expression": "1+1"}, ctx)).status == "budget"


async def test_document_tools_enforce_ownership(make_client, db, settings):
    a, b = await make_client(), await make_client()
    await register(a)
    doc = (await a.post("/api/v1/documents", files={"file": ("p.pdf", make_pdf(PHOTO_PAGES), "application/pdf")})).json()
    ctx = await ctx_for(b, db, settings)  # registers user b and uses b's identity
    assert str(ctx.user["_id"]) != (await a.get("/api/v1/users/me")).json()["id"]
    r = await REGISTRY.execute("get_passages", {"document_id": doc["id"]}, ctx)
    assert r.status == "error" and "not found" in r.error.lower()
    r = await REGISTRY.execute("summarize_document", {"document_id": doc["id"]}, ctx)
    assert r.status == "error"
    r = await REGISTRY.execute("search_documents", {"query": "calvin cycle stroma", "document_ids": [doc["id"]]}, ctx)
    assert r.status == "ok" and r.output["status"] == "no_documents" and r.output["hits"] == []


async def test_search_and_summarize_own_document(client, db, settings):
    ctx = await ctx_for(client, db, settings)
    doc = (await client.post("/api/v1/documents", files={"file": ("p.pdf", make_pdf(PHOTO_PAGES), "application/pdf")})).json()
    r = await REGISTRY.execute("search_documents", {"query": "calvin cycle stroma"}, ctx)
    assert r.status == "ok" and r.output["hits"][0]["page"] == 2
    p = await REGISTRY.execute("get_passages", {"document_id": doc["id"], "page": 1}, ctx)
    assert p.status == "ok" and all(x["page"] == 1 for x in p.output["passages"])
    s = await REGISTRY.execute("summarize_document", {"document_id": doc["id"]}, ctx)
    assert s.status == "ok" and s.output["summary"]


async def test_tool_calls_are_audited_without_argument_values(client, db, settings):
    ctx = await ctx_for(client, db, settings)
    await REGISTRY.execute("calculate", {"expression": "123456789+1"}, ctx)
    ev = await db.audit_events.find_one({"type": "tool_call"})
    assert ev["tool"] == "calculate" and ev["arg_keys"] == ["expression"] and "123456789" not in json.dumps(ev, default=str)


def test_registry_metadata_complete():
    for t in REGISTRY.describe():
        assert t["description"] and t["permission"] in STUDENT_PERMISSIONS and t["timeout_s"] > 0
        assert t["args_schema"]["additionalProperties"] is False
    assert {t["name"]: t["read_only"] for t in REGISTRY.describe()}["save_preference"] is False


# ---------------- Groq client (mock transport) ----------------
def groq_ok(content='{"ok": true}', usage=None):
    return {"choices": [{"message": {"content": content, "reasoning": "SECRET CHAIN OF THOUGHT"}, "finish_reason": "stop"}],
            "usage": usage or {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}, "model": "openai/gpt-oss-120b"}


def client_with(handler, **over):
    return LLMClient(make_settings(max_retries=2, **over), transport=httpx.MockTransport(handler))


async def test_groq_request_pins_model_and_ignores_reasoning(monkeypatch):
    seen = {}

    def handler(req):
        seen["body"] = json.loads(req.content)
        seen["auth"] = req.headers["authorization"]
        return httpx.Response(200, json=groq_ok())

    res = await client_with(handler).chat([{"role": "user", "content": "hi"}], json_mode=True)
    assert seen["body"]["model"] == "openai/gpt-oss-120b" and seen["body"]["response_format"] == {"type": "json_object"}
    assert res.usage["total"] == 15 and "CHAIN" not in res.text
    assert seen["auth"].startswith("Bearer gsk_test")


async def test_groq_retries_then_succeeds(monkeypatch):
    import asyncio
    async def nosleep(*_): return None
    monkeypatch.setattr(asyncio, "sleep", nosleep)
    n = {"c": 0}

    def handler(req):
        n["c"] += 1
        return httpx.Response(429, headers={"retry-after": "0"}, json={}) if n["c"] < 3 else httpx.Response(200, json=groq_ok())

    assert (await client_with(handler).chat([{"role": "user", "content": "x"}])).text
    assert n["c"] == 3


async def test_groq_retry_budget_is_bounded(monkeypatch):
    import asyncio
    async def nosleep(*_): return None
    monkeypatch.setattr(asyncio, "sleep", nosleep)
    n = {"c": 0}

    def handler(req):
        n["c"] += 1
        return httpx.Response(429, json={})

    with pytest.raises(RateLimitError):
        await client_with(handler).chat([{"role": "user", "content": "x"}])
    assert n["c"] == 3  # 1 + max_retries(2)


async def test_groq_errors_map_to_safe_exceptions(monkeypatch):
    import asyncio
    async def nosleep(*_): return None
    monkeypatch.setattr(asyncio, "sleep", nosleep)
    msg = [{"role": "user", "content": "x"}]
    with pytest.raises(ConfigError):
        await client_with(lambda r: httpx.Response(401, json={})).chat(msg)
    with pytest.raises(ProviderError) as e:
        await client_with(lambda r: httpx.Response(404, json={"error": "model_not_found"})).chat(msg)
    assert "no substitute" in str(e.value)
    with pytest.raises(ProviderError):
        await client_with(lambda r: httpx.Response(200, json={"choices": []})).chat(msg)
    with pytest.raises(ProviderError):
        await client_with(lambda r: httpx.Response(200, json=groq_ok(content="  "))).chat(msg)

    def boom(req):
        raise httpx.ConnectTimeout("t")
    with pytest.raises(ProviderError):
        await client_with(boom).chat(msg)
    with pytest.raises(ConfigError):
        await LLMClient(make_settings(groq_api_key="")).chat(msg)


async def test_budgeted_llm_enforces_token_budget_and_repairs():
    fake = FakeLLM()
    b = BudgetedLLM(fake, budget_tokens=100)
    await b.chat([{"role": "user", "content": "x"}])
    assert b.used > 0
    await b.chat([{"role": "user", "content": "x"}])
    with pytest.raises(TokenBudgetExceeded):
        await b.chat([{"role": "user", "content": "x"}])
    fake2 = FakeLLM()
    fake2.push("garbage", '{"answer": "fixed"}')
    out = await BudgetedLLM(fake2, 5000).structured([{"role": "user", "content": "q"}], __import__("app.schemas.learning", fromlist=["TutorOut"]).TutorOut)
    assert out.answer == "fixed" and "invalid" in fake2.calls[1][-1]["content"]


async def test_groq_json_validate_failed_is_retried_with_more_room():
    calls = []

    def handler(req):
        body = json.loads(req.content)
        calls.append(body["max_completion_tokens"])
        if len(calls) == 1:  # truncated JSON: provider answers 400 json_validate_failed
            return httpx.Response(400, json={"error": {"code": "json_validate_failed", "message": "model output cut",
                                                       "failed_generation": '{"answer": "par'}})
        return httpx.Response(200, json=groq_ok())

    res = await client_with(handler, llm_max_output_tokens=1000).chat([{"role": "user", "content": "hi"}], json_mode=True)
    assert calls == [1000, 2000] and res.text == '{"ok": true}'


async def test_groq_other_400_is_a_safe_provider_error():
    from app.core.errors import ProviderError
    handler = lambda req: httpx.Response(400, json={"error": {"code": "bad_request", "message": "nope"}})
    with pytest.raises(ProviderError) as e:
        await client_with(handler).chat([{"role": "user", "content": "hi"}])
    assert "HTTP 400" in str(e.value)
