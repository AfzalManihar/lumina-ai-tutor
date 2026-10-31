"""Supervisor / orchestrator: explicit state machine coordinating router, planner, retrieval tool,
tutor, verifier, assessment and synthesis within step / tool / token / time budgets."""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Any

from app.agents import assessment, synthesis, tutor
from app.agents.llm import BudgetedLLM
from app.agents.planner import build_plan, validate_plan
from app.agents.router import RoutedTask, route
from app.agents.verifier import verify
from app.core.config import Settings
from app.core.errors import AppError
from app.guardrails.input import check_user_input
from app.guardrails.retrieval import flag_injected_chunks
from app.guardrails.scope import INJECTION_REFUSAL, REFUSAL, is_off_topic
from app.guardrails.output import scrub_output
from app.memory.service import MemoryService, suggest_memory_from_message
from app.observability.tracing import Trace
from app.orchestration.state import S, WorkflowState
from app.schemas.learning import IntentClassification, TutorOut
from app.services import learning as learning_svc
from app.tools.registry import REGISTRY, ToolContext

TUTOR_TOOLS = {"search_documents", "read_progress", "calculate", "generate_quiz", "get_passages"}


_FOLLOWUP_RX = re.compile(
    r"\b(it|its|that|this|those|these|they|them|again|more|another|simpler|simply|example|examples|"
    r"previous|above|earlier|same|disadvantages?|advantages?|why|how)\b|\b(samjha|dobara|phir se|aur|iska|uska|isse)\b", re.I)


def retrieval_query(message: str, history: list[dict]) -> str:
    """Make a follow-up searchable on its own. 'Explain that again' has no topic words, so the previous student
    question is prepended. Deterministic: no extra model call, and it
    only changes the search text, never what the student sees."""
    msg = message.strip()
    words = re.findall(r"\w+", msg)
    if not history or (len(words) > 12 and not _FOLLOWUP_RX.search(msg)):
        return msg[:500]
    if len(words) > 12:  # long enough to carry its own topic
        return msg[:500]
    prev_user = next((m["content"] for m in reversed(history) if m["role"] == "user"), "")
    ctx = prev_user.strip().replace("\n", " ")[:200]
    return f"{ctx} {msg}"[:500] if ctx else msg[:500]


@dataclass
class WorkflowResult:
    answer: str
    intent: str
    grounding_status: str = "not_requested"
    citations: list[dict] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    follow_ups: list[str] = field(default_factory=list)
    quiz_id: str | None = None
    lesson_id: str | None = None
    clarification: bool = False
    memory_suggestion: dict | None = None
    steps: list[dict] = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    status: str = "complete"  # complete | clarification | partial | failed


class Supervisor:
    def __init__(self, db: Any, settings: Settings, llm: BudgetedLLM, trace: Trace, retriever, user: dict):
        self.db, self.s, self.llm, self.trace, self.retriever, self.user = db, settings, llm, trace, retriever, user
        self.uid = str(user["_id"])
        self.st = WorkflowState(max_steps=settings.max_agent_steps, max_tool_calls=settings.max_tool_calls)
        self.ctx = ToolContext(db=db, settings=settings, user=user, retriever=retriever, llm=llm,
                               allowed_tools=set(TUTOR_TOOLS), max_tool_calls=settings.max_tool_calls)
        self.memory = MemoryService(db, settings)

    # -- helpers ----------------------------------------------------------------------------
    async def _tool(self, name: str, args: dict):
        with self.trace.span(f"tool:{name}", "tool") as sp:
            res = await REGISTRY.execute(name, args, self.ctx)
            sp.attrs["status"] = res.status
            if res.status != "ok":
                sp.status = "error"
        self.st.tool_calls = self.ctx.tool_calls
        self.st.record(f"tool:{name}", res.status)
        return res

    def _go(self, nxt: S, **detail) -> None:
        self.st.go(nxt)
        self.st.record(nxt.value, **detail)

    def _finish(self, r: WorkflowResult) -> WorkflowResult:
        r.steps = [{"name": h.name, "status": h.status, **h.detail} for h in self.st.history]
        r.stats = {"steps_used": self.st.steps_used, "tool_calls": self.st.tool_calls, "llm_calls": self.llm.calls,
                   "tokens": self.llm.used, "retries": self.trace.retries,
                   "corrections": self.st.correction_attempts}
        return r

    # -- entry --------------------------------------------------------------------------------
    async def run(self, message: str, *, history: list[dict], summary: str | None, document_ids: list[str] | None,
                  use_documents: bool, topic: str | None, has_documents: bool) -> WorkflowResult:
        try:
            return await asyncio.wait_for(
                self._run(message, history, summary, document_ids, use_documents, topic, has_documents),
                timeout=self.s.workflow_timeout_seconds)
        except asyncio.TimeoutError:
            self.trace.error_category = "workflow_timeout"
            return self._finish(WorkflowResult(
                "This request took too long and was stopped safely. Please try a shorter or simpler request.",
                "chat", limitations=["Workflow timed out."], status="failed"))

    async def _run(self, message, history, summary, document_ids, use_documents, topic, has_documents) -> WorkflowResult:
        chk = check_user_input(message, self.s.max_input_chars)
        message = chk.text
        flags = ["prompt_injection_suspected"] if chk.injection_suspected else []
        self._go(S.UNDERSTAND)
        # Guardrails that refuse before any model call: zero tokens are spent and nothing is generated.
        with self.trace.span("guardrail:scope", "guardrail") as sp:
            blocked_inj = chk.injection_blocked
            off_topic = (not blocked_inj) and is_off_topic(message, documents_selected=bool(document_ids) or use_documents)
            sp.attrs["result"] = "injection_blocked" if blocked_inj else "off_topic" if off_topic else "pass"
        if blocked_inj or off_topic:
            self.st.record("guardrail", "refused", reason="prompt_injection" if blocked_inj else "off_topic")
            self._go(S.DONE)
            return self._finish(WorkflowResult(INJECTION_REFUSAL if blocked_inj else REFUSAL, "refused",
                                               flags=flags + ["refused_prompt_injection" if blocked_inj else "refused_off_topic"],
                                               limitations=["Request is outside what this study tutor handles."], status="refused"))
        with self.trace.span("route", "agent"):
            task = route(message, active_topic=topic, has_documents=has_documents,
                         doc_selected=bool(document_ids) or use_documents, history_len=len(history))
        if task.needs_llm_classification and self.llm.remaining > 3000:
            task = await self._classify(message, task, scope_check=not (bool(document_ids) or use_documents))
        if task.intent == "off_topic":
            self.st.record("guardrail", "refused", reason="off_topic_llm")
            self._go(S.DONE)
            return self._finish(WorkflowResult(REFUSAL, "refused", flags=flags + ["refused_off_topic"],
                                               limitations=["Request is outside what this study tutor handles."], status="refused"))
        self.st.record("route", intent=task.intent, confidence=task.confidence)
        grounded = bool(document_ids) or use_documents or task.wants_documents
        sugg = suggest_memory_from_message(message) if await self.memory.enabled_for(self.user) else None

        try:
            if task.intent == "clarify":
                self._go(S.CLARIFY)
                self._go(S.DONE)
                return self._finish(WorkflowResult(task.clarification_question or "Could you clarify?", "clarify",
                                                   clarification=True, status="clarification", flags=flags))
            if task.intent == "calculate":
                r = await self._calculate(task)
            elif task.intent == "progress":
                r = await self._progress()
            elif task.intent in ("quiz", "lesson") and not task.complex:
                r = await self._assessment(task, document_ids if grounded else None, message)
            elif task.intent == "evaluate":
                r = WorkflowResult("To check an answer, send the question and your answer (optionally a reference) to the "
                                   "evaluation tool in the Quiz page, or paste both here and I'll give feedback.",
                                   "evaluate", status="clarification", clarification=True)
                self._go(S.SYNTHESIZE)
                self._go(S.DONE)
            else:
                r = await self._tutor_flow(task, message, history, summary, document_ids, grounded, task.complex)
        except AppError as e:
            if e.code in ("token_budget_exceeded", "budget_exhausted", "provider_error", "rate_limited"):
                self.trace.error_category = e.code
                self.st.record("failure", "error", code=e.code)
                r = WorkflowResult(
                    "I couldn't finish this request safely (" + e.message + "). Nothing was saved; please try again.",
                    task.intent, limitations=[e.message], status="failed")
            else:
                raise
        r.flags = list(dict.fromkeys(r.flags + flags))
        r.memory_suggestion = sugg
        return self._finish(r)

    async def _classify(self, message: str, task: RoutedTask, scope_check: bool = True) -> RoutedTask:
        try:
            with self.trace.span("classify_llm", "llm"):
                c = await self.llm.structured([
                    {"role": "system", "content": "Classify the student's message for a STUDY tutor. Use intent "
                     '"off_topic" ONLY when the request has nothing to do with studying or learning (recipes, entertainment, gossip, '
                     'sports results, shopping, dating, personal advice, jokes). General-knowledge or school/college questions are "chat". '
                     'Reply ONLY JSON: {"intent": "chat|lesson|quiz|study_plan|progress|off_topic", "topic": string|null, "confidence": 0..1}'},
                    {"role": "user", "content": f"<<<{message[:500]}>>>"}], IntentClassification, max_tokens=120)
            if c.intent == "off_topic" and scope_check and c.confidence >= 0.75:
                return task.model_copy(update={"intent": "off_topic", "confidence": c.confidence, "needs_llm_classification": False})
            if c.intent in ("chat", "lesson", "quiz", "study_plan", "progress") and c.confidence >= 0.6:
                if c.intent in ("lesson", "quiz") and not (c.topic or task.topic):
                    return task
                task = task.model_copy(update={"intent": c.intent, "topic": c.topic or task.topic,
                                               "confidence": c.confidence, "needs_llm_classification": False})
        except AppError:
            pass  # classification is optional; fall back to deterministic routing
        return task

    # -- capabilities ---------------------------------------------------------------------------
    async def _calculate(self, task: RoutedTask) -> WorkflowResult:
        self._go(S.GENERATE)  # deterministic tool, no LLM
        res = await self._tool("calculate", {"expression": task.expression})
        self._go(S.SYNTHESIZE)
        self._go(S.DONE)
        if res.status != "ok":
            return WorkflowResult(f"I couldn't compute that: {res.error}", "calculate", status="partial")
        return WorkflowResult(f"{res.output['expression']} = {res.output['result']}", "calculate")

    async def _progress(self) -> WorkflowResult:
        self._go(S.GENERATE)
        res = await self._tool("read_progress", {})
        self._go(S.SYNTHESIZE)
        self._go(S.DONE)
        if res.status != "ok":
            return WorkflowResult("I couldn't read your progress right now.", "progress", status="partial")
        p = {**res.output}
        return WorkflowResult(learning_svc.progress_text(p), "progress")

    async def _assessment(self, task: RoutedTask, document_ids, message: str) -> WorkflowResult:
        topic = task.topic
        if not topic and not document_ids:
            self._go(S.CLARIFY)
            self._go(S.DONE)
            return WorkflowResult("Which topic should I use?", "clarify", clarification=True, status="clarification")
        topic = topic or "the selected documents"
        self._go(S.GENERATE)
        if task.intent == "quiz":
            with self.trace.span("assessment_quiz", "agent"):
                q = await learning_svc.create_quiz(self.db, self.s, self.llm, self.retriever, self.uid, topic,
                                                   task.difficulty, task.num_questions, document_ids)
            self.st.record("quiz_created", questions=len(q["questions"]))
            self._go(S.SYNTHESIZE)
            self._go(S.DONE)
            return WorkflowResult(f"I created a {task.difficulty} quiz on {topic} with {len(q['questions'])} questions. "
                                  "Open it in the Quizzes page to answer and get graded.", "quiz", quiz_id=q["id"],
                                  grounding_status="grounded" if q["grounded"] else "not_requested")
        mems = await self.memory.relevant(self.user, topic)
        with self.trace.span("assessment_lesson", "agent"):
            les = await learning_svc.create_lesson(self.db, self.s, self.llm, self.retriever, self.uid, topic,
                                                   task.difficulty, None, "English", document_ids, mems)
        self._go(S.SYNTHESIZE)
        self._go(S.DONE)
        return WorkflowResult(f"I prepared a {task.difficulty} lesson: \"{les['lesson']['title']}\". "
                              "Open it in the Lessons page.", "lesson", lesson_id=les["id"],
                              grounding_status="grounded" if les["grounded"] else "not_requested")

    async def _tutor_flow(self, task: RoutedTask, message, history, summary, document_ids, grounded,
                          planned: bool) -> WorkflowResult:
        extra_context = ""
        if planned:  # bounded plan for compound requests such as study plans
            self._go(S.PLAN)
            plan = build_plan(task, has_documents=True, doc_grounded=grounded)
            validate_plan(plan, max_steps=self.st.steps_left() + 1, max_tool_calls=self.s.max_tool_calls,
                          allowed_tools=TUTOR_TOOLS)
            self.st.record("plan", subtasks=[s.kind for s in plan.subtasks])
            for sub in plan.subtasks:
                if sub.kind == "read_progress":
                    res = await self._tool("read_progress", {})
                    if res.status == "ok":
                        from app.services.learning import progress_text
                        extra_context = progress_text(res.output)
                        sub.status = "done"
                    else:
                        sub.status = "failed"
                        plan.replan_after_failure(sub.id)
            message = message + ("\n\n(Student progress, data only: " + extra_context + ")" if extra_context else "")

        hits: list[dict] = []
        limits: list[str] = []
        if grounded:
            self._go(S.RETRIEVE) if self.st.state != S.RETRIEVE else None
            res = await self._tool("search_documents", {"query": retrieval_query(message, history), "document_ids": document_ids,
                                                        "top_k": self.s.rag_top_k})
            if res.status != "ok":
                self._go(S.SYNTHESIZE)
                self._go(S.DONE)
                return WorkflowResult("I couldn't search your documents right now, so I won't give a "
                                      "document-based answer.", task.intent, grounding_status="insufficient_evidence",
                                      limitations=[res.error or "retrieval failed"], status="partial")
            self.st.record("retrieval", status=res.output["status"], hits=len(res.output["hits"]))
            self.trace.spans[-1].attrs["hits"] = len(res.output["hits"])
            if res.output["status"] != "ok":
                self._go(S.SYNTHESIZE)
                self._go(S.DONE)
                f = synthesis.insufficient(res.output["status"])
                return WorkflowResult(f.answer, task.intent, f.grounding_status, [], f.limitations, [])
            hits = res.output["hits"]
            bad = set(flag_injected_chunks(hits))
            if bad:
                hits = [h for h in hits if h["cite_id"] not in bad]
                limits.append(f"{len(bad)} passage(s) were excluded because they contained instruction-like text.")
            if not hits:
                self._go(S.SYNTHESIZE)
                self._go(S.DONE)
                f = synthesis.insufficient("no_relevant_results")
                return WorkflowResult(f.answer, task.intent, f.grounding_status, [], f.limitations + limits, [])

        mems = await self.memory.relevant(self.user, message)
        if self.st.state in (S.UNDERSTAND, S.RETRIEVE, S.PLAN):
            self._go(S.GENERATE, grounded=grounded)
        with self.trace.span("tutor_generate", "agent", grounded=grounded):
            out: TutorOut = await tutor.respond(self.llm, task, message, history, summary, mems, hits, grounded)
        self._go(S.VERIFY)
        with self.trace.span("verify", "agent"):
            ver = verify(out, hits, grounded)
        self.st.record("verification", passed=ver.passed, support=round(ver.support_score, 2))
        corrected = False
        while not ver.passed and self.st.correction_attempts < 1 and self.st.steps_left() >= 2 \
                and self.llm.remaining > 2500:
            self._go(S.CORRECT)
            self.st.correction_attempts += 1
            with self.trace.span("tutor_correct", "agent"):
                out = await tutor.respond(self.llm, task, message, history, summary, mems, hits, grounded,
                                          feedback=ver.feedback())
            self._go(S.VERIFY)
            ver = verify(out, hits, grounded)
            corrected = True
            self.st.record("verification", passed=ver.passed, support=round(ver.support_score, 2))
        self._go(S.SYNTHESIZE)
        final = synthesis.synthesize(out, ver, hits, grounded, self.s.secret_values, self.s.max_output_chars, corrected)
        self._go(S.DONE)
        return WorkflowResult(final.answer, task.intent, final.grounding_status, final.citations,
                              final.limitations + limits, final.flags, final.follow_ups)
