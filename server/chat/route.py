from fastapi import APIRouter, HTTPException, Depends, Body
from auth.route import authenticate
from chat.chat_query import answer_query, generate_from_retrieval, quiz_generation
from chat.schemas import TutorChatRequest, TutorQuizRequest
from chat.tutor import answer_tutor_query, generate_topic_quiz, TutorProviderError
from chat import agentic, tools
from config.settings import ConfigurationError, get_setting
from pydantic import BaseModel, ConfigDict, Field
from typing import Literal, List, Optional
import time
import datetime
import uuid
import logging
import os
import traceback
from observability.tracing import TraceEvent, emit_trace
from config.db import chat_history_collection, quizzes_collection, quiz_history

router = APIRouter()
logger = logging.getLogger("uvicorn.error")

STUDY_ONLY_MESSAGE = (
    "I can only help with study-related questions, academic subjects, "
    "homework, and learning activities."
)


def _check_study_query(query: str) -> dict:
    """Reject unsafe or off-topic requests before calling a model."""
    try:
        gate = agentic.guardrail_agent(query)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if not isinstance(gate, dict):
        logger.error("Guardrail returned an invalid result")
        raise HTTPException(
            status_code=500,
            detail="The study-topic validation service is unavailable.",
        )

    # Fail closed: only explicitly allowed requests may proceed.
    if gate.get("action") != "allow":
        raise HTTPException(status_code=400, detail=STUDY_ONLY_MESSAGE)

    normalized_query = gate.get("query")
    if not isinstance(normalized_query, str) or not normalized_query.strip():
        raise HTTPException(
            status_code=400,
            detail="Please enter a valid study-related question.",
        )

    return gate


def _log_tutor_exception(exc: Exception, trace_id: str) -> None:
    """Log exception types and traceback locations without request contents."""
    chain = []
    frames = []
    current = exc
    seen = set()

    while current is not None and id(current) not in seen:
        seen.add(id(current))
        name = type(current).__name__
        chain.append(name)

        if current.__traceback__:
            frames.append(name + ":")
            frames.extend(
                f"  {os.path.basename(frame.filename)}:{frame.lineno} in {frame.name}"
                for frame in traceback.extract_tb(current.__traceback__)
            )

        current = current.__cause__ or current.__context__

    logger.error(
        "Tutor request failed; trace_id=%s; exception_chain=%s\n%s",
        trace_id,
        " -> ".join(chain),
        "\n".join(frames),
    )


class MemorySettingsRequest(BaseModel):
    enabled: bool
    preferred_level: Literal["Beginner", "Intermediate", "Advanced"] = "Beginner"
    learning_goal: str = Field(default="", max_length=160)


class QuizRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    topic: str = Field(min_length=2, max_length=200)
    num_questions: Optional[int] = Field(default=3, ge=1, le=10)


class QuizAnswerRequest(BaseModel):
    quiz_id: str = Field(min_length=1, max_length=40)
    answers: List[str] = Field(min_length=1, max_length=10)


@router.post("/chat")
async def chat(
    user=Depends(authenticate),
    query: str = Body(..., embed=True, min_length=1, max_length=3000),
):
    if user["role"] != "Student":
        raise HTTPException(
            status_code=403,
            detail="Only students can ask questions",
        )

    gate = _check_study_query(query)
    study_query = gate["query"]

    try:
        response = await answer_query(
            study_query,
            user["role"],
            user["grade"],
            user["user_id"],
        )
    except ConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Document question service failed")
        raise HTTPException(
            status_code=502,
            detail="The document question service is unavailable",
        ) from exc

    try:
        chat_history_collection.insert_one(
            {
                "user_id": user["user_id"],
                "timestamp": datetime.datetime.now(datetime.timezone.utc),
                "query": study_query,
                "response": response["answer"],
                "sources": response.get("sources", []),
                "citations": response.get("citations", []),
            }
        )
        response["history_saved"] = True
    except Exception:
        logger.exception("Could not save chat history")
        response["history_saved"] = False

    return response


@router.post("/quiz")
async def quiz(request: QuizRequest, user=Depends(authenticate)):
    if user["role"] != "Student":
        raise HTTPException(
            status_code=403,
            detail="Only students can generate quizzes",
        )

    gate = _check_study_query(request.topic)
    study_topic = gate["query"]

    try:
        response = await quiz_generation(
            study_topic,
            user["role"],
            user["grade"],
            request.num_questions,
            user["user_id"],
        )
    except ConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Document quiz service failed")
        raise HTTPException(
            status_code=502,
            detail="The document quiz service is unavailable",
        ) from exc

    quiz_doc = {
        "user_id": user["user_id"],
        "timestamp": datetime.datetime.now(datetime.timezone.utc),
        "topic": study_topic,
        "quiz_data": response["quiz"],
        "sources": response.get("sources", []),
    }

    try:
        result = quizzes_collection.insert_one(quiz_doc)
    except Exception as exc:
        logger.exception("Could not save generated quiz")
        raise HTTPException(
            status_code=503,
            detail="The quiz could not be saved",
        ) from exc

    return {
        "quiz": response["quiz"],
        "sources": response.get("sources", []),
        "quiz_id": str(result.inserted_id),
    }


@router.post("/quiz/check")
async def check_quiz_answers(
    request: QuizAnswerRequest,
    user=Depends(authenticate),
):
    try:
        from bson.objectid import ObjectId
        quiz_object_id = ObjectId(request.quiz_id)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid quiz id") from exc

    quiz_doc = quizzes_collection.find_one(
        {"_id": quiz_object_id, "user_id": user["user_id"]}
    )

    if not quiz_doc:
        raise HTTPException(status_code=404, detail="Quiz not found")

    correct_answers = [
        line.split(":", 1)[1].strip()[0].upper()
        for line in quiz_doc["quiz_data"].splitlines()
        if line.startswith("Correct Answer:")
        and line.split(":", 1)[1].strip()
    ]

    if not correct_answers or len(request.answers) != len(correct_answers):
        raise HTTPException(
            status_code=400,
            detail="Answer count does not match quiz question count",
        )

    score = 0
    results = []

    for index, answer in enumerate(request.answers):
        normalized_answer = answer.strip().upper()
        is_correct = normalized_answer == correct_answers[index]
        score += int(is_correct)

        results.append(
            {
                "question_number": index + 1,
                "user_answer": answer,
                "correct_answer": correct_answers[index],
                "is_correct": is_correct,
            }
        )

    quiz_history.insert_one(
        {
            "user_id": user["user_id"],
            "quiz_id": request.quiz_id,
            "timestamp": datetime.datetime.now(datetime.timezone.utc),
            "topic": quiz_doc["topic"],
            "score": score,
            "total": len(correct_answers),
            "results": results,
            "quiz_content": quiz_doc["quiz_data"],
        }
    )

    return {
        "message": f"Quiz complete, You scored {score}/{len(correct_answers)}",
        "score": score,
        "total": len(correct_answers),
        "results": results,
    }


@router.get("/quiz/history")
async def get_quiz_history(user=Depends(authenticate)):
    if user["role"] != "Student":
        raise HTTPException(
            status_code=403,
            detail="Only students can view quiz history",
        )

    cursor = quiz_history.find(
        {"user_id": user["user_id"]}
    ).sort("timestamp", -1)

    history = []

    for doc in cursor:
        doc["id"] = str(doc.pop("_id"))
        doc["quiz_id"] = str(doc["quiz_id"])
        doc.pop("user_id", None)
        history.append(doc)

    return {
        "message": f"Found {len(history)} attempts",
        "history": history,
    }


@router.post("/tutor/chat")
async def tutor_chat(
    request: TutorChatRequest,
    user=Depends(authenticate),
):
    started = time.perf_counter()

    if user["role"] != "Student":
        raise HTTPException(
            status_code=403,
            detail="Only students can use the tutor",
        )

    # Validate before routing, memory, tools, budget reservation, or LLM calls.
    gate = _check_study_query(request.query)
    study_query = gate["query"]

    use_documents = (
        request.ground_in_documents
        or agentic.document_grounding_requested(study_query)
    )

    routing = agentic.query_router(study_query, use_documents)

    memory = agentic.memory_agent(
        user["user_id"],
        study_query,
        request.level,
        request.remember_memory,
    )

    workflow = agentic.WorkflowState(
        trace_id=str(uuid.uuid4()),
        user_id=user["user_id"],
        route=routing["intent"],
    )

    agentic.advance(workflow, agentic.Stage.GUARDED)
    agentic.advance(workflow, agentic.Stage.MEMORY)

    study_plan = (
        agentic.planning_agent(study_query)
        if routing["planner_required"]
        else None
    )

    if study_plan:
        agentic.advance(workflow, agentic.Stage.PLANNED)
        memory["context"] += (
            " Bounded study plan: " + "; ".join(study_plan.steps)
        )

    history = [item.model_dump() for item in request.history]

    estimated = (
        0
        if routing["intent"] == "math" and not use_documents
        else agentic.estimate_tokens(
            study_query
            + memory["context"]
            + " ".join(item["content"] for item in history)
        )
        + 1400
        + (4000 if use_documents else 0)
    )

    try:
        reservation = agentic.reserve_budget(
            user["user_id"],
            estimated,
        )
    except agentic.TokenBudgetExceeded as exc:
        raise HTTPException(
            status_code=429,
            detail={
                "message": str(exc),
                "used_tokens": exc.used,
                "monthly_limit": exc.limit,
                "estimated_request_tokens": exc.requested,
                "retry_after": "next monthly reset or reduce context",
            },
        ) from exc
    except Exception as exc:
        logger.exception("Could not reserve tutor usage budget")
        raise HTTPException(
            status_code=503,
            detail="Usage limits are temporarily unavailable",
        ) from exc

    try:
        if use_documents:
            agentic.advance(workflow, agentic.Stage.RETRIEVED)

            retrieval_result = await tools.execute_tool(
                "search_learning_materials",
                {"query": study_query},
                tools.ToolContext(
                    user_id=user["user_id"],
                    role=user["role"],
                    grade=user.get("grade"),
                ),
            )

            result = await generate_from_retrieval(
                study_query,
                retrieval_result,
            )

            result["tool_result"] = {
                "status": retrieval_result["status"],
                "retrieval_count": retrieval_result["retrieval_count"],
            }

        elif (
            routing["intent"] == "math"
            and routing["math_expression"]
        ):
            tool_result = await tools.execute_tool(
                "calculate",
                {"expression": routing["math_expression"]},
                tools.ToolContext(
                    user_id=user["user_id"],
                    role=user["role"],
                    grade=user.get("grade"),
                ),
            )

            result = {
                "answer": (
                    f"{routing['math_expression']} = {tool_result['result']}. "
                    "I used the verified arithmetic tool so you can check "
                    "the calculation directly."
                ),
                "sources": [],
                "citations": [],
                "evidence": [],
                "tool_result": tool_result,
            }

        else:
            result = await answer_tutor_query(
                study_query,
                request.level,
                history,
                memory["context"],
            )

        agentic.advance(workflow, agentic.Stage.GENERATED)

    except ConfigurationError as exc:
        agentic.release_budget(reservation)
        workflow.error_category = "configuration"
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    except TutorProviderError as exc:
        _log_tutor_exception(exc, workflow.trace_id)
        agentic.release_budget(reservation)
        workflow.error_category = "provider"
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    except Exception as exc:
        _log_tutor_exception(exc, workflow.trace_id)
        agentic.release_budget(reservation)
        workflow.error_category = "workflow"
        raise HTTPException(
            status_code=502,
            detail="The tutor service is unavailable",
        ) from exc

    if study_plan:
        result["answer"] += (
            "\n\nSuggested study plan:\n"
            + "\n".join(
                f"{i + 1}. {step}"
                for i, step in enumerate(study_plan.steps)
            )
        )

    citations = result.get("citations", [])

    try:
        answer = agentic.validate_output(result["answer"])
    except ValueError as exc:
        _log_tutor_exception(exc, workflow.trace_id)
        agentic.release_budget(reservation)
        raise HTTPException(
            status_code=502,
            detail="Tutor response failed output safety validation",
        ) from exc

    verification = agentic.verify_grounded_answer(
        answer,
        citations,
        result.get("evidence", []),
    )

    if use_documents:
        logger.warning(
            "RAG verification diagnostics: trace_id=%s citations=%s "
            "citation_validity=%s evidence_coverage=%s unsupported_claims=%s",
            workflow.trace_id,
            len(citations),
            verification.get("citation_validity"),
            verification.get("evidence_coverage"),
            verification.get("unsupported_claims"),
        )

    evaluation = agentic.evaluation_agent(
        answer,
        result.get("usage"),
        estimated,
        bool(citations),
    )

    if use_documents and verification["needs_revision"]:
        answer = (
            "I couldn't verify enough information in your uploaded study "
            "material to answer this. Try asking about a specific concept "
            "or page from the PDF."
        )
        citations = []
        result["sources"] = []

        verification = {
            **verification,
            "supported": False,
            "citation_validity": False,
            "verification_status": "insufficient_evidence",
            "needs_revision": False,
        }

    evaluation["verification"] = verification
    evaluation["accuracy_status"] = (
        verification["verification_status"]
        if use_documents
        else "not_fact_checked"
    )

    agentic.advance(workflow, agentic.Stage.VERIFIED)
    agentic.advance(workflow, agentic.Stage.COMPLETE)

    now = datetime.datetime.now(datetime.timezone.utc)
    history_saved = True

    try:
        chat_history_collection.insert_one(
            {
                "user_id": user["user_id"],
                "timestamp": now,
                "query": study_query,
                "response": answer,
                "level": request.level,
                "sources": result.get("sources", []),
                "citations": citations,
                "tokens": result.get("usage"),
                "token_estimate": estimated,
                "evaluation": evaluation,
                "agent_trace": [
                    stage.value for stage in workflow.steps
                ],
                "provider": "groq",
                "model": get_setting(
                    "GROQ_MODEL",
                    required=False,
                    default="openai/gpt-oss-120b",
                ),
                "trace_id": workflow.trace_id,
                "latency_ms": round(
                    (time.perf_counter() - started) * 1000
                ),
            }
        )
    except Exception:
        logger.exception("Could not save tutor chat history")
        history_saved = False

    usage_saved = True

    try:
        agentic.commit_budget(
            reservation,
            evaluation["tokens"],
            evaluation["token_count_source"],
            "groq",
            (
                "rag_chat"
                if use_documents
                else (
                    "math_tool"
                    if routing["intent"] == "math"
                    else "tutor_chat"
                )
            ),
        )
    except Exception:
        logger.exception("Could not commit tutor usage budget")
        agentic.release_budget(reservation)
        usage_saved = False

    usage_state = None

    try:
        usage_state = agentic.monthly_usage(user["user_id"])
    except Exception:
        logger.exception("Could not fetch monthly usage")

    trace = TraceEvent(
        trace_id=workflow.trace_id,
        user_ref=__import__("hashlib")
        .sha256(user["user_id"].encode())
        .hexdigest()[:16],
        workflow=routing["intent"],
        steps=[stage.value for stage in workflow.steps],
        retrieval_count=len(result.get("evidence", [])),
        latency_ms=round(
            (time.perf_counter() - started) * 1000
        ),
        token_count=evaluation["tokens"],
        token_source=evaluation["token_count_source"],
        verification_status=verification["verification_status"],
    )

    emit_trace(trace)

    return {
        "answer": answer,
        "citations": citations,
        "sources": result.get("sources", []),
        "history_saved": history_saved,
        "usage": result.get("usage"),
        "usage_saved": usage_saved,
        "usage_summary": usage_state,
        "evaluation": evaluation,
        "accuracy_status": evaluation["accuracy_status"],
        "trace_id": workflow.trace_id,
        "route": routing["intent"],
        "response_complete": result.get("response_complete", True),
        "study_plan": (
            study_plan.model_dump() if study_plan else None
        ),
        "agents": [
            {
                "name": "Supervisor",
                "status": (
                    "completed in "
                    + str(len(workflow.steps))
                    + " bounded steps"
                ),
            },
            {
                "name": "Query router",
                "status": routing["intent"],
            },
            {
                "name": "Planner",
                "status": (
                    "bounded plan created"
                    if study_plan
                    else "not required"
                ),
            },
            {
                "name": "Guardrails",
                "status": (
                    "flagged for safe handling"
                    if gate.get("risk") != "normal"
                    else "passed"
                ),
            },
            {
                "name": "Memory",
                "status": (
                    "personalized"
                    if memory["topics"]
                    else (
                        "saved with consent"
                        if request.remember_memory
                        else "read-only/opt-in"
                    )
                ),
            },
            {
                "name": "RAG",
                "status": (
                    "retrieved and cited"
                    if use_documents
                    else "not requested"
                ),
            },
            {
                "name": "Tool",
                "status": result.get("tool_result", {}).get(
                    "status", "not used"
                ),
            },
            {
                "name": "Tutor",
                "status": "answered",
            },
            {
                "name": "Evaluator",
                "status": evaluation["accuracy_status"],
            },
        ],
        "provider": "groq",
        "model": get_setting(
            "GROQ_MODEL",
            required=False,
            default="openai/gpt-oss-120b",
        ),
        "latency_ms": round(
            (time.perf_counter() - started) * 1000
        ),
        "workflow_steps": [
            stage.value for stage in workflow.steps
        ],
    }


@router.get("/usage")
async def get_usage(user=Depends(authenticate)):
    if user["role"] != "Student":
        raise HTTPException(
            status_code=403,
            detail="Only students can view usage",
        )

    try:
        return {
            "usage": agentic.monthly_usage(user["user_id"]),
            "request_limit": agentic.REQUEST_TOKEN_LIMIT,
        }
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail="Usage history is unavailable",
        ) from exc


@router.get("/learning/accuracy")
async def get_learning_accuracy(user=Depends(authenticate)):
    if user["role"] != "Student":
        raise HTTPException(
            status_code=403,
            detail="Only students can view learning accuracy",
        )

    try:
        return agentic.learning_accuracy(user["user_id"])
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail="Quiz evaluation history is unavailable",
        ) from exc


@router.get("/memory")
async def get_learning_memory(user=Depends(authenticate)):
    if user["role"] != "Student":
        raise HTTPException(
            status_code=403,
            detail="Only students can manage memory",
        )

    from config.db import learner_memory_collection

    doc = learner_memory_collection.find_one(
        {"user_id": user["user_id"]}
    ) or {}

    return {
        "enabled": bool(doc.get("enabled")),
        "preferred_level": doc.get("preferred_level", "Beginner"),
        "learning_goal": doc.get("learning_goal", ""),
        "topics": (
            list((doc.get("topic_counts") or {}).keys())[-20:]
            if doc.get("enabled") is True
            else []
        ),
        "updated_at": doc.get("updated_at"),
    }


@router.put("/memory")
async def update_learning_memory(
    request: MemorySettingsRequest,
    user=Depends(authenticate),
):
    if user["role"] != "Student":
        raise HTTPException(
            status_code=403,
            detail="Only students can manage memory",
        )

    from config.db import learner_memory_collection

    learner_memory_collection.update_one(
        {"user_id": user["user_id"]},
        {
            "$set": {
                "user_id": user["user_id"],
                "enabled": request.enabled,
                "preferred_level": request.preferred_level,
                "learning_goal": request.learning_goal,
                "provenance": "explicit learner preference",
                "updated_at": datetime.datetime.now(
                    datetime.timezone.utc
                ),
            }
        },
        upsert=True,
    )

    return {
        "enabled": request.enabled,
        "message": "Memory preferences saved",
    }


@router.delete("/memory")
async def delete_learning_memory(user=Depends(authenticate)):
    if user["role"] != "Student":
        raise HTTPException(
            status_code=403,
            detail="Only students can manage memory",
        )

    from config.db import learner_memory_collection

    result = learner_memory_collection.delete_one(
        {"user_id": user["user_id"]}
    )

    return {
        "deleted": bool(result.deleted_count),
        "message": "Saved learning memory deleted",
    }


@router.post("/tutor/quiz")
async def tutor_quiz(
    request: TutorQuizRequest,
    user=Depends(authenticate),
):
    if user["role"] != "Student":
        raise HTTPException(
            status_code=403,
            detail="Only students can generate quizzes",
        )

    # Validate topic before reserving budget or calling the quiz model.
    gate = _check_study_query(request.topic)
    study_topic = gate["query"]

    quiz_estimate = (
        agentic.estimate_tokens(study_topic)
        + request.num_questions * 650
    )

    try:
        quiz_reservation = agentic.reserve_budget(
            user["user_id"],
            quiz_estimate,
        )
    except agentic.TokenBudgetExceeded as exc:
        raise HTTPException(
            status_code=429,
            detail={
                "message": str(exc),
                "used_tokens": exc.used,
                "monthly_limit": exc.limit,
                "estimated_request_tokens": exc.requested,
            },
        ) from exc
    except Exception as exc:
        logger.exception("Could not reserve quiz usage budget")
        raise HTTPException(
            status_code=503,
            detail="Usage limits are temporarily unavailable",
        ) from exc

    quiz_trace_id = str(uuid.uuid4())

    try:
        quiz = await generate_topic_quiz(
            study_topic,
            request.level,
            request.num_questions,
        )
    except ConfigurationError as exc:
        agentic.release_budget(quiz_reservation)
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except TutorProviderError as exc:
        _log_tutor_exception(exc, quiz_trace_id)
        agentic.release_budget(quiz_reservation)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:
        _log_tutor_exception(exc, quiz_trace_id)
        agentic.release_budget(quiz_reservation)
        raise HTTPException(
            status_code=502,
            detail="The quiz service is unavailable",
        ) from exc

    quiz_data = "\n\n".join(
        f"Question {i + 1}: {q.question}\n"
        + "\n".join(
            f"{chr(65 + j)}) {option}"
            for j, option in enumerate(q.options)
        )
        + f"\nCorrect Answer: {chr(65 + q.correct_option_index)}"
        for i, q in enumerate(quiz.questions)
    )

    try:
        stored = quizzes_collection.insert_one(
            {
                "user_id": user["user_id"],
                "timestamp": datetime.datetime.now(
                    datetime.timezone.utc
                ),
                "topic": study_topic,
                "quiz_data": quiz_data,
                "structured_quiz": quiz.model_dump(),
                "sources": [],
            }
        )
    except Exception as exc:
        agentic.release_budget(quiz_reservation)
        logger.exception("Could not save tutor quiz")
        raise HTTPException(
            status_code=503,
            detail="The generated quiz could not be saved",
        ) from exc

    try:
        agentic.commit_budget(
            quiz_reservation,
            quiz_estimate,
            "estimated",
            "groq",
            "quiz_generation",
        )
    except Exception:
        logger.exception("Could not commit quiz usage budget")
        agentic.release_budget(quiz_reservation)

    return {
        "quiz_id": str(stored.inserted_id),
        "topic": study_topic,
        "questions": [
            {
                "question": q.question,
                "options": q.options,
                "hint": q.hint,
            }
            for q in quiz.questions
        ],
    }


@router.post("/tutor/quiz/check")
async def check_tutor_quiz(
    request: QuizAnswerRequest,
    user=Depends(authenticate),
):
    try:
        from bson.objectid import ObjectId
        quiz_id = ObjectId(request.quiz_id)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid quiz id") from exc

    quiz_doc = quizzes_collection.find_one(
        {"_id": quiz_id, "user_id": user["user_id"]}
    )

    if not quiz_doc:
        raise HTTPException(status_code=404, detail="Quiz not found")

    quiz = quiz_doc.get("structured_quiz")

    if not quiz or len(request.answers) != len(quiz["questions"]):
        raise HTTPException(
            status_code=400,
            detail="Answer count does not match quiz question count",
        )

    results = []
    score = 0

    for question, answer in zip(quiz["questions"], request.answers):
        answer = answer.strip().upper()
        options = question["options"]
        correct_index = question["correct_option_index"]

        if (
            len(answer) != 1
            or not answer.isalpha()
            or ord(answer) - 65 >= len(options)
        ):
            raise HTTPException(
                status_code=400,
                detail="Answers must use an available option letter",
            )

        correct = ord(answer) - 65 == correct_index
        score += int(correct)

        results.append(
            {
                "question": question["question"],
                "user_answer": answer,
                "correct_answer": chr(65 + correct_index),
                "is_correct": correct,
                "explanation": question["explanation"],
            }
        )

    now = datetime.datetime.now(datetime.timezone.utc)

    quiz_history.insert_one(
        {
            "user_id": user["user_id"],
            "quiz_id": request.quiz_id,
            "timestamp": now,
            "topic": quiz_doc["topic"],
            "score": score,
            "total": len(results),
            "results": results,
        }
    )

    return {
        "score": score,
        "total": len(results),
        "results": results,
        "history_saved": True,
    }


@router.get("/learning/history")
async def learning_history(user=Depends(authenticate)):
    if user["role"] != "Student":
        raise HTTPException(
            status_code=403,
            detail="Only students can view learning history",
        )

    try:
        chats = list(
            chat_history_collection.find(
                {"user_id": user["user_id"]},
                {
                    "_id": 0,
                    "query": 1,
                    "response": 1,
                    "timestamp": 1,
                    "sources": 1,
                },
            )
            .sort("timestamp", -1)
            .limit(30)
        )

        quizzes = list(
            quiz_history.find(
                {"user_id": user["user_id"]},
                {
                    "_id": 0,
                    "topic": 1,
                    "score": 1,
                    "total": 1,
                    "timestamp": 1,
                },
            )
            .sort("timestamp", -1)
            .limit(30)
        )
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail="Learning history storage is unavailable",
        ) from exc

    entries = [
        {
            "type": "tutor",
            "topic": item.get("query", "Tutor session"),
            "detail": item.get("response", "")[:300],
            "timestamp": item.get("timestamp"),
            "sources": item.get("sources", []),
        }
        for item in chats
    ] + [
        {
            "type": "quiz",
            "topic": item.get("topic", "Practice quiz"),
            "detail": (
                f"{item.get('score', 0)}/{item.get('total', 0)} correct"
            ),
            "timestamp": item.get("timestamp"),
            "sources": [],
        }
        for item in quizzes
    ]

    entries.sort(
        key=lambda item: (
            item["timestamp"].timestamp()
            if hasattr(item.get("timestamp"), "timestamp")
            else 0
        ),
        reverse=True,
    )

    try:
        topics = set(
            chat_history_collection.distinct(
                "query",
                {"user_id": user["user_id"]},
            )
        )
        topics.update(
            quiz_history.distinct(
                "topic",
                {"user_id": user["user_id"]},
            )
        )

        totals = {
            "topics": len(topics),
            "tutor_messages": chat_history_collection.count_documents(
                {"user_id": user["user_id"]}
            ),
            "quiz_attempts": quiz_history.count_documents(
                {"user_id": user["user_id"]}
            ),
        }
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail="Learning history totals are unavailable",
        ) from exc

    return {
        "history": entries[:50],
        "totals": totals,
    }