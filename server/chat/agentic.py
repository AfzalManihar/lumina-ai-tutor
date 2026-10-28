"""Typed orchestration primitives and deterministic safety/evaluation agents."""
from __future__ import annotations

import datetime as dt
import os
import re
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field
from config.db import (
    ai_usage_collection,
    quiz_history,
    usage_budget_collection,
    subscriptions_collection,
)

MONTHLY_TOKEN_LIMIT = 250_000
REQUEST_TOKEN_LIMIT = 8_000
MAX_WORKFLOW_STEPS = max(6, min(10, int(os.getenv("MAX_AGENT_STEPS", "8"))))


class Stage(StrEnum):
    RECEIVED = "received"
    GUARDED = "guarded"
    MEMORY = "memory"
    PLANNED = "planned"
    RETRIEVED = "retrieved"
    GENERATED = "generated"
    VERIFIED = "verified"
    COMPLETE = "complete"
    FAILED = "failed"


TRANSITIONS = {
    Stage.RECEIVED: {Stage.GUARDED, Stage.FAILED},
    Stage.GUARDED: {
        Stage.MEMORY, Stage.RETRIEVED, Stage.GENERATED, Stage.FAILED
    },
    Stage.MEMORY: {
        Stage.PLANNED, Stage.RETRIEVED, Stage.GENERATED, Stage.FAILED
    },
    Stage.PLANNED: {Stage.RETRIEVED, Stage.GENERATED, Stage.FAILED},
    Stage.RETRIEVED: {Stage.GENERATED, Stage.FAILED},
    Stage.GENERATED: {Stage.VERIFIED, Stage.FAILED},
    Stage.VERIFIED: {Stage.COMPLETE, Stage.FAILED},
    Stage.COMPLETE: set(),
    Stage.FAILED: set(),
}


class WorkflowState(BaseModel):
    trace_id: str
    user_id: str
    route: Literal["tutor", "document_qa", "math"]
    stage: Stage = Stage.RECEIVED
    steps: list[Stage] = Field(
        default_factory=lambda: [Stage.RECEIVED],
        max_length=MAX_WORKFLOW_STEPS,
    )
    started_at: dt.datetime = Field(
        default_factory=lambda: dt.datetime.now(dt.timezone.utc)
    )
    retries: int = 0
    error_category: str | None = None


def advance(state: WorkflowState, stage: Stage) -> WorkflowState:
    if len(state.steps) >= MAX_WORKFLOW_STEPS:
        raise ValueError("Agent workflow step budget exceeded")
    if stage not in TRANSITIONS[state.stage]:
        raise ValueError(
            f"Invalid workflow transition: {state.stage} -> {stage}"
        )
    state.stage = stage
    state.steps.append(stage)
    return state


class VerificationResult(BaseModel):
    supported: bool
    unsupported_claims: list[str] = Field(default_factory=list)
    citation_validity: bool
    evidence_coverage: float = Field(ge=0, le=1)
    verification_status: Literal[
        "passed", "needs_review", "insufficient_evidence"
    ]
    needs_revision: bool


class TokenBudgetExceeded(RuntimeError):
    def __init__(self, used: int, limit: int, requested: int):
        self.used = used
        self.limit = limit
        self.requested = requested
        super().__init__(
            "Estimated request would exceed your monthly token budget"
        )


def estimate_tokens(text: str) -> int:
    return max(1, (len(text) + 2) // 3)


def document_grounding_requested(query: str) -> bool:
    """Recognize clear learner references to uploaded study sources."""
    text = " ".join(query.lower().split())
    source = (
        r"(?:pdf|document|notes?|study material|uploaded material|file|source)"
    )
    reference = r"(?:my|uploaded|upload|attached|provided|this|the)"

    return bool(
        re.search(
            rf"\b{reference}\b.{{0,48}}\b{source}\b|"
            rf"\b{source}\b.{{0,48}}\b{reference}\b|"
            rf"\baccording to\b.{{0,24}}\b{source}\b",
            text,
        )
    )


def query_router(query: str, use_documents: bool) -> dict:
    """Low-cost deterministic intent router; not an authorization check."""
    math_expression = re.search(
        r"(?<![\w.])\d+(?:\.\d+)?\s*[+*/%-]\s*\d+(?:\.\d+)?"
        r"(?:\s*[+*/%-]\s*\d+(?:\.\d+)?)*",
        query,
    )

    return {
        "intent": (
            "document_qa"
            if use_documents
            else ("math" if math_expression else "tutor")
        ),
        "topic": query.strip()[:200],
        "needs_retrieval": bool(use_documents),
        "needs_memory": True,
        "planner_required": (
            len(query) > 900
            or query.lower().startswith(
                ("make a study plan", "plan my exam")
            )
        ),
        "math_expression": (
            math_expression.group(0) if math_expression else None
        ),
    }


class StudyPlan(BaseModel):
    objective: str = Field(min_length=3, max_length=200)
    steps: list[str] = Field(min_length=2, max_length=4)
    estimated_minutes: int = Field(ge=5, le=120)


def planning_agent(query: str) -> StudyPlan:
    """Bounded deterministic study task planner."""
    topic = query.strip()[:120]

    return StudyPlan(
        objective=topic,
        steps=[
            "Identify the key idea and starting point",
            "Study one worked example",
            "Attempt a practice question",
            "Review any incorrect step",
        ],
        estimated_minutes=25,
    )


def guardrail_agent(query: str) -> dict:
    """
    First-pass deterministic study-topic guardrail.

    Important: the API route must reject requests when action is
    'reject' or 'reject_off_topic'.
    """
    cleaned = " ".join(query.split())

    if not cleaned:
        raise ValueError("Please enter a question")

    if len(cleaned) > 3000:
        raise ValueError("Question is too long")

    # Detect common prompt-injection attempts.
    injection_patterns = (
        r"\bignore\b.{0,50}\b(previous|prior|system)\s+instructions?\b",
        r"\b(reveal|show|print|expose)\b.{0,50}\b"
        r"(system prompt|api key|secret)\b",
        r"\b(bypass|disable|override)\b.{0,50}\b"
        r"(safety|guardrails|rules)\b",
    )

    flagged = any(
        re.search(pattern, cleaned, re.IGNORECASE)
        for pattern in injection_patterns
    )

    if flagged:
        return {
            "query": cleaned,
            "risk": "prompt_injection",
            "action": "reject",
            "is_study_related": False,
            "message": (
                "I can help with study-related questions only. "
                "Please ask an academic question."
            ),
        }

    # Explicitly off-topic requests.
    # Generic words like "explain" do not automatically allow a request.
    off_topic_patterns = (
        r"\b(bake|baking|cook|cooking|recipe|recipes|cake|cookies)\b",
        r"\b(movie|movies|film|tv show|celebrity|gossip|lyrics)\b",
        r"\b(book a flight|travel itinerary|tourist attractions|"
        r"vacation plan|hotel recommendations)\b",
        r"\b(password cracking|steal credentials|malware deployment)\b",
    )

    if any(
        re.search(pattern, cleaned, re.IGNORECASE)
        for pattern in off_topic_patterns
    ):
        return {
            "query": cleaned,
            "risk": "normal",
            "action": "reject_off_topic",
            "is_study_related": False,
            "message": (
                "I can help with study-related questions only. "
                "Please ask an academic question."
            ),
        }

    # Recognize common academic subjects and specific learning activities.
    study_patterns = (
        r"\b(math|mathematics|algebra|geometry|calculus|arithmetic|"
        r"equation|theorem|fraction|statistics|probability)\b",
        r"\b(physics|chemistry|biology|science|photosynthesis|gravity|"
        r"atom|cell|electricity|force|chemical reaction)\b",
        r"\b(history|geography|economics|civics|political science)\b",
        r"\b(english|grammar|literature|vocabulary|linguistics|"
        r"translation|punctuation)\b",
        r"\b(computer science|programming|coding|python|javascript|"
        r"machine learning|artificial intelligence|data science|"
        r"algorithm|database|sql|software engineering)\b",
        r"\b(homework|school assignment|college assignment|"
        r"exam preparation|syllabus|textbook|study notes|"
        r"academic research|practice question|study plan|coursework|"
        r"lecture notes)\b",
        r"\b(solve|calculate|definition|formula|proof)\b",
        r"\b(explain the concept of|explain the difference between|"
        r"what is the meaning of)\b",
    )

    is_study_related = any(
        re.search(pattern, cleaned, re.IGNORECASE)
        for pattern in study_patterns
    )

    return {
        "query": cleaned,
        "risk": "normal",
        "action": "allow" if is_study_related else "reject_off_topic",
        "is_study_related": is_study_related,
        "message": (
            ""
            if is_study_related
            else "I can help with study-related questions only. "
            "Please ask an academic question."
        ),
    }


def validate_output(answer: str) -> str:
    answer = str(answer).strip()

    if not answer or len(answer) > 12000:
        raise ValueError(
            "Tutor output is empty or exceeds the response limit"
        )

    secret_patterns = (
        r"(?i)gsk_[A-Za-z0-9]{16,}",
        r"(?i)sk-[A-Za-z0-9]{20,}",
        r"(?i)mongodb(?:\+srv)?://[^\s]+",
    )

    if any(re.search(pattern, answer) for pattern in secret_patterns):
        raise ValueError("Tutor output failed secret-leak validation")

    return answer


def verify_grounded_answer(
    answer: str,
    citations: list[dict],
    evidence: list[dict],
) -> dict:
    valid_pairs = {
        (str(item.get("filename", "")), int(item.get("page", -1)))
        for item in citations
    }

    citation_refs = re.findall(
        r"\[([^\],]+),\s*page\s*(\d+)\]",
        answer,
        re.IGNORECASE,
    )

    citation_validity = (
        all(
            (name.strip(), int(page)) in valid_pairs
            for name, page in citation_refs
        )
        if citation_refs
        else False
    )

    evidence_text = " ".join(
        str(item.get("text", "")) for item in evidence
    ).lower()

    evidence_terms = set(re.findall(r"[a-z0-9]{4,}", evidence_text))

    sentences = [
        sentence.strip()
        for sentence in re.split(r"(?<=[.!?])\s+", answer)
        if len(sentence.split()) >= 5
    ]

    unsupported = []

    for sentence in sentences:
        terms = {
            word
            for word in re.findall(r"[a-z0-9]{4,}", sentence.lower())
            if word not in {
                "that", "this", "with", "from", "then", "than",
                "which", "your", "have", "will", "there", "their",
                "because",
            }
        }

        coverage = len(terms & evidence_terms) / max(1, len(terms))

        if coverage < 0.18:
            unsupported.append(sentence[:400])

    supported = bool(evidence) and citation_validity and not unsupported

    return {
        "supported": supported,
        "unsupported_claims": unsupported,
        "citation_validity": citation_validity,
        "evidence_coverage": round(
            (len(sentences) - len(unsupported)) / max(1, len(sentences)),
            3,
        ),
        "verification_status": (
            "passed"
            if supported
            else ("needs_review" if evidence else "insufficient_evidence")
        ),
        "needs_revision": bool(evidence) and not supported,
    }


def memory_agent(
    user_id: str,
    query: str,
    level: str,
    consent: bool = False,
) -> dict:
    """Read relevant memory; persist only after explicit learner opt-in."""
    try:
        from config.db import learner_memory_collection

        raw = learner_memory_collection.find_one(
            {"user_id": user_id}
        ) or {}

        doc = raw if raw.get("enabled") is True else {}

        terms = set(re.findall(r"[a-z0-9]{4,}", query.lower()))
        counts = doc.get("topic_counts") or {}

        ranked = sorted(
            counts,
            key=lambda topic: (
                len(terms & set(topic.split("_"))),
                counts[topic],
            ),
            reverse=True,
        )

        relevant = [
            topic for topic in ranked
            if terms & set(topic.split("_"))
        ][:3]

        safe_label = re.sub(
            r"(?i)\b[\w.+-]+@[\w.-]+\.[a-z]{2,}\b",
            " ",
            query,
        )
        safe_label = re.sub(
            r"(?<!\w)\+?\d[\d ()-]{7,}\d(?!\w)",
            " ",
            safe_label,
        )

        sensitive = re.search(
            r"(?i)(password|passcode|api[ _-]?key|secret|access token|gsk_|sk-)",
            query,
        )

        if (consent or doc.get("enabled") is True) and not sensitive:
            slug = re.sub(
                r"[^a-z0-9]+",
                "_",
                safe_label.lower(),
            ).strip("_")[:48] or "general"

            fields = {
                "user_id": user_id,
                "enabled": True,
                "preferred_level": level,
                "last_topic": slug,
                "provenance": "explicit learner opt-in",
                "updated_at": dt.datetime.now(dt.timezone.utc),
            }

            if raw.get("enabled") is True:
                learner_memory_collection.update_one(
                    {"user_id": user_id},
                    {
                        "$set": fields,
                        "$inc": {f"topic_counts.{slug}": 1},
                    },
                )
            else:
                fields["topic_counts"] = {slug: 1}
                learner_memory_collection.update_one(
                    {"user_id": user_id},
                    {"$set": fields},
                    upsert=True,
                )

        profile = []

        if doc.get("preferred_level"):
            profile.append(
                "Preferred level: " + str(doc["preferred_level"])
            )

        if doc.get("learning_goal"):
            profile.append(
                "Learner-approved goal: "
                + str(doc["learning_goal"])[:160]
            )

        if relevant:
            profile.append(
                "Previously explored relevant topics: "
                + ", ".join(relevant)
            )

        return {
            "topics": relevant,
            "context": (
                " ".join(profile)
                if profile
                else "No relevant saved learning memory."
            ),
            "consent": bool(doc.get("enabled") or consent),
        }

    except Exception:
        return {
            "topics": [],
            "context": "No saved learning memory.",
            "consent": False,
        }


def _legacy_usage(user_id: str, start: dt.datetime) -> int:
    docs = ai_usage_collection.find(
        {
            "user_id": user_id,
            "created_at": {"$gte": start},
        },
        {"total_tokens": 1},
    )

    return sum(int(item.get("total_tokens", 0)) for item in docs)


def monthly_token_limit(user_id: str) -> int:
    limit = max(
        1,
        int(os.getenv("USER_MONTHLY_TOKEN_LIMIT", str(MONTHLY_TOKEN_LIMIT))),
    )

    try:
        subscription = subscriptions_collection.find_one(
            {"user_id": user_id, "status": "active"}
        )

        if subscription and subscription.get("plan") == "pro":
            limit = max(
                limit,
                int(os.getenv("SUBSCRIPTION_TOKEN_LIMIT_PRO", "1000000")),
            )

    except Exception:
        pass

    return limit


def monthly_usage(user_id: str) -> dict:
    now = dt.datetime.now(dt.timezone.utc)
    start = now.replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )
    limit = monthly_token_limit(user_id)

    account = usage_budget_collection.find_one(
        {
            "user_id": user_id,
            "period_start": start.date().isoformat(),
        }
    )

    if account:
        used = int(account.get("used_tokens", 0))
        reserved = int(account.get("reserved_tokens", 0))
    else:
        used = _legacy_usage(user_id, start)
        reserved = 0

    billable = used + reserved

    return {
        "period_start": start.date().isoformat(),
        "used_tokens": used,
        "reserved_tokens": reserved,
        "monthly_limit": limit,
        "remaining_tokens": max(0, limit - billable),
        "usage_percent": round(min(100, billable / limit * 100), 1),
    }


def reserve_budget(user_id: str, estimate: int) -> dict:
    now = dt.datetime.now(dt.timezone.utc)
    start = now.replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )
    period = start.date().isoformat()
    limit = monthly_token_limit(user_id)

    if estimate < 0 or estimate > REQUEST_TOKEN_LIMIT:
        state = monthly_usage(user_id)
        raise TokenBudgetExceeded(
            state["used_tokens"], limit, estimate
        )

    existing = usage_budget_collection.find_one(
        {"user_id": user_id, "period_start": period}
    )

    if not existing:
        legacy = _legacy_usage(user_id, start)

        try:
            usage_budget_collection.update_one(
                {"user_id": user_id, "period_start": period},
                {
                    "$setOnInsert": {
                        "used_tokens": legacy,
                        "reserved_tokens": 0,
                        "created_at": now,
                    }
                },
                upsert=True,
            )
        except Exception:
            if not usage_budget_collection.find_one(
                {"user_id": user_id, "period_start": period}
            ):
                raise

    reserved = usage_budget_collection.find_one_and_update(
        {
            "user_id": user_id,
            "period_start": period,
            "$expr": {
                "$lte": [
                    {
                        "$add": [
                            "$used_tokens",
                            "$reserved_tokens",
                            estimate,
                        ]
                    },
                    limit,
                ]
            },
        },
        {"$inc": {"reserved_tokens": estimate}},
        return_document=True,
    )

    if not reserved:
        state = monthly_usage(user_id)
        raise TokenBudgetExceeded(
            state["used_tokens"] + state["reserved_tokens"],
            limit,
            estimate,
        )

    return {
        "user_id": user_id,
        "period_start": period,
        "reserved_tokens": estimate,
    }


def release_budget(reservation: dict) -> None:
    if reservation and reservation.get("reserved_tokens"):
        usage_budget_collection.update_one(
            {
                "user_id": reservation["user_id"],
                "period_start": reservation["period_start"],
            },
            {
                "$inc": {
                    "reserved_tokens": -int(
                        reservation["reserved_tokens"]
                    )
                }
            },
        )


def commit_budget(
    reservation: dict,
    actual: int,
    source: str,
    provider: str,
    kind: str,
) -> None:
    amount = max(0, int(actual))

    usage_budget_collection.update_one(
        {
            "user_id": reservation["user_id"],
            "period_start": reservation["period_start"],
        },
        {
            "$inc": {
                "reserved_tokens": -int(reservation["reserved_tokens"]),
                "used_tokens": amount,
            }
        },
    )

    try:
        ai_usage_collection.insert_one(
            {
                "user_id": reservation["user_id"],
                "created_at": dt.datetime.now(dt.timezone.utc),
                "total_tokens": amount,
                "source": source,
                "provider": provider,
                "request_kind": kind,
            }
        )
    except Exception:
        # The atomic budget account is authoritative; event detail is secondary.
        pass


def evaluation_agent(
    answer: str,
    usage: dict | None,
    estimated: int,
    grounded: bool,
) -> dict:
    words = len(answer.split())

    rubric = {
        "substantive": words >= 20,
        "stepwise": any(
            item in answer.lower()
            for item in ("step", "because", "example", "first", "next")
        ),
        "grounded": grounded,
    }

    actual = None

    if isinstance(usage, dict):
        actual = usage.get("total_tokens") or usage.get("totalTokens")

        if not actual:
            actual = (
                int(
                    usage.get(
                        "input_tokens",
                        usage.get("prompt_tokens", 0),
                    ) or 0
                )
                + int(
                    usage.get(
                        "output_tokens",
                        usage.get("completion_tokens", 0),
                    ) or 0
                )
            ) or None

    return {
        "rubric_score": round(
            100 * sum(rubric.values()) / len(rubric)
        ),
        "rubric": rubric,
        "accuracy_status": (
            "source-grounded"
            if grounded
            else "not independently verified"
        ),
        "tokens": int(actual) if actual else estimated,
        "token_count_source": "provider" if actual else "estimated",
    }


def learning_accuracy(user_id: str) -> dict:
    docs = list(
        quiz_history.find(
            {"user_id": user_id},
            {"score": 1, "total": 1},
        )
    )

    correct = sum(int(item.get("score", 0)) for item in docs)
    total = sum(int(item.get("total", 0)) for item in docs)

    return {
        "quiz_attempts": len(docs),
        "correct_answers": correct,
        "total_answers": total,
        "accuracy_percent": (
            round(correct / total * 100, 1) if total else None
        ),
        "accuracy_status": (
            "measured_from_quiz_answers"
            if total
            else "not_enough_quiz_data"
        ),
    }


def track_estimated_usage(
    user_id: str,
    tokens: int,
    provider: str,
    kind: str,
) -> None:
    ai_usage_collection.insert_one(
        {
            "user_id": user_id,
            "created_at": dt.datetime.now(dt.timezone.utc),
            "total_tokens": int(tokens),
            "source": "estimated",
            "provider": provider,
            "request_kind": kind,
        }
    )