
"""Owner-scoped course, lesson, and progress API."""

import logging
import traceback
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile

from auth.route import authenticate
from config.db import (
    chunk_collection,
    course_quiz_history,
    courses_collection,
    lesson_progress_collection,
)
from config.settings import ConfigurationError
from courses.generation import GenerationError, generate_curriculum, generate_lesson
from courses.schemas import (
    CourseCreateRequest,
    CurriculumOutput,
    LessonOutput,
    LessonProgressRequest,
    LessonResponse,
    PracticeAnswerRequest,
    QuizAnswerRequest,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/courses", tags=["courses"])
MAX_SOURCE_BYTES = 15 * 1024 * 1024


def _object_id(value: str):
    try:
        from bson import ObjectId
        return ObjectId(value)
    except Exception as exc:
        raise HTTPException(
            status_code=404,
            detail="Course or lesson not found",
        ) from exc


def _get_owned_course(course_id: str, user_id: str):
    doc = courses_collection.find_one(
        {"_id": _object_id(course_id), "owner_id": user_id}
    )
    if not doc:
        raise HTTPException(status_code=404, detail="Course not found")
    return doc


def _public_course(doc: dict) -> dict:
    course = {
        key: value
        for key, value in doc.items()
        if key not in {"owner_id", "_id"}
    }
    course["course_id"] = str(doc["_id"])

    modules = []
    for module in course.get("modules", []):
        modules.append({
            "title": module["title"],
            "summary": module["summary"],
            "lessons": [
                {key: value for key, value in lesson.items() if key != "content"}
                for lesson in module.get("lessons", [])
            ],
        })

    course["modules"] = modules
    return course


def _source_context(source_id: str | None, owner_id: str) -> str:
    if not source_id:
        return ""

    chunks = list(
        chunk_collection.find(
            {"doc_id": source_id, "owner_id": owner_id},
            {"text": 1, "page": 1, "_id": 0},
        ).sort("page", 1)
    )

    if not chunks:
        raise HTTPException(
            status_code=404,
            detail="Uploaded source not found",
        )

    return "\n\n".join(chunk["text"] for chunk in chunks)[:40000]


def _generation_error(exc: Exception):
    if isinstance(exc, ConfigurationError):
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if isinstance(exc, GenerationError):
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    raise exc


def _public_lesson(course: dict, lesson: dict, module_title: str):
    if not lesson.get("content"):
        raise HTTPException(
            status_code=409,
            detail="Lesson has not been generated yet",
        )

    content = LessonOutput.model_validate(lesson["content"])

    return LessonResponse(
        lesson_id=lesson["lesson_id"],
        module_title=module_title,
        title=lesson["title"],
        summary=lesson["summary"],
        content={
            "explanation": content.explanation,
            "worked_examples": [
                item.model_dump() for item in content.worked_examples
            ],
            "practice_questions": [
                {"question": item.question, "hint": item.hint}
                for item in content.practice_questions
            ],
            "quiz": [
                {"question": item.question, "options": item.options}
                for item in content.quiz
            ],
        },
    )


def _require_generated_content(lesson: dict) -> LessonOutput:
    if not lesson.get("content"):
        raise HTTPException(
            status_code=409,
            detail="Lesson has not been generated yet",
        )
    return LessonOutput.model_validate(lesson["content"])


def _find_lesson(course: dict, lesson_id: str):
    for module in course.get("modules", []):
        for lesson in module.get("lessons", []):
            if lesson.get("lesson_id") == lesson_id:
                return module, lesson

    raise HTTPException(status_code=404, detail="Lesson not found")


@router.post("/sources")
async def upload_course_source(
    file: UploadFile = File(...),
    user=Depends(authenticate),
):
    """Index an optional private PDF source for this learner's courses."""

    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(
            status_code=400,
            detail="Only PDF files are supported",
        )

    content = await file.read(MAX_SOURCE_BYTES + 1)

    if len(content) > MAX_SOURCE_BYTES:
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
    source_id = str(uuid4())

    try:
        from docs.vectorstore import load_vectorstore

        await load_vectorstore(
            uploaded_files=[file],
            role="Private",
            doc_id=source_id,
            grade=int(user.get("grade") or 0),
            owner_id=user["user_id"],
        )

    except ValueError as exc:
        logger.warning("PDF validation failed: %s", exc)
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    except ConfigurationError as exc:
        logger.exception("PDF indexing configuration error")
        raise HTTPException(
            status_code=503,
            detail=str(exc),
        ) from exc

    except Exception as exc:
        logger.error(
            "PDF indexing failed for source_id=%s; error_type=%s; error=%s",
            source_id,
            type(exc).__name__,
            exc,
        )
        logger.error("Full PDF indexing traceback:\n%s", traceback.format_exc())

        raise HTTPException(
            status_code=503,
            detail="Could not process the PDF source; check backend logs for the original error",
        ) from exc

    return {
        "source_id": source_id,
        "filename": file.filename,
        "status": "indexed",
    }


@router.post("")
def create_course(request: CourseCreateRequest, user=Depends(authenticate)):
    owner_id = user["user_id"]
    context = _source_context(request.source_id, owner_id)

    try:
        curriculum = generate_curriculum(
            topic=request.topic.strip(),
            level=request.level,
            learning_goal=request.learning_goal.strip(),
            context=context,
        )
    except (ConfigurationError, GenerationError) as exc:
        _generation_error(exc)

    modules = []
    for module in curriculum.modules:
        modules.append({
            "title": module.title,
            "summary": module.summary,
            "lessons": [
                {
                    "lesson_id": str(uuid4()),
                    "title": lesson.title,
                    "summary": lesson.summary,
                    "content": None,
                }
                for lesson in module.lessons
            ],
        })

    now = datetime.now(timezone.utc)
    course_doc = {
        "owner_id": owner_id,
        "topic": request.topic.strip(),
        "level": request.level,
        "learning_goal": request.learning_goal.strip(),
        "source_id": request.source_id,
        "objectives": curriculum.objectives,
        "modules": modules,
        "created_at": now,
        "updated_at": now,
    }

    result = courses_collection.insert_one(course_doc)
    course_doc["_id"] = result.inserted_id

    return _public_course(course_doc)


@router.get("")
def list_courses(user=Depends(authenticate)):
    docs = courses_collection.find(
        {"owner_id": user["user_id"]}
    ).sort("updated_at", -1)

    return {"courses": [_public_course(doc) for doc in docs]}


@router.get("/{course_id}")
def get_course(course_id: str, user=Depends(authenticate)):
    course = _get_owned_course(course_id, user["user_id"])

    progress = list(
        lesson_progress_collection.find(
            {
                "owner_id": user["user_id"],
                "course_id": str(course["_id"]),
            },
            {
                "_id": 0,
                "lesson_id": 1,
                "completed": 1,
                "completed_at": 1,
            },
        )
    )

    result = _public_course(course)
    result["progress"] = progress
    result["quiz_attempts"] = list(
        course_quiz_history.find(
            {
                "owner_id": user["user_id"],
                "course_id": str(course["_id"]),
            },
            {"_id": 0, "owner_id": 0},
        ).sort("created_at", -1)
    )

    return result


@router.post("/{course_id}/lessons/{lesson_id}/generate")
def generate_course_lesson(
    course_id: str,
    lesson_id: str,
    user=Depends(authenticate),
):
    course = _get_owned_course(course_id, user["user_id"])
    module, lesson = _find_lesson(course, lesson_id)

    if lesson.get("content"):
        return _public_lesson(course, lesson, module["title"])

    context = _source_context(
        course.get("source_id"),
        user["user_id"],
    )

    try:
        generated = generate_lesson(
            topic=course["topic"],
            level=course["level"],
            learning_goal=course.get("learning_goal", ""),
            objectives=course["objectives"],
            module_title=module["title"],
            lesson_title=lesson["title"],
            lesson_summary=lesson["summary"],
            context=context,
        )
    except (ConfigurationError, GenerationError) as exc:
        _generation_error(exc)

    courses_collection.update_one(
        {
            "_id": course["_id"],
            "owner_id": user["user_id"],
            "modules.lessons.lesson_id": lesson_id,
        },
        {
            "$set": {
                "modules.$[].lessons.$[lesson].content": generated.model_dump(),
                "updated_at": datetime.now(timezone.utc),
            }
        },
        array_filters=[{"lesson.lesson_id": lesson_id}],
    )

    lesson["content"] = generated.model_dump()
    return _public_lesson(course, lesson, module["title"])


@router.get("/{course_id}/lessons/{lesson_id}")
def get_course_lesson(
    course_id: str,
    lesson_id: str,
    user=Depends(authenticate),
):
    course = _get_owned_course(course_id, user["user_id"])
    module, lesson = _find_lesson(course, lesson_id)
    return _public_lesson(course, lesson, module["title"])


@router.post("/{course_id}/lessons/{lesson_id}/progress")
def set_lesson_progress(
    course_id: str,
    lesson_id: str,
    request: LessonProgressRequest,
    user=Depends(authenticate),
):
    course = _get_owned_course(course_id, user["user_id"])
    _, lesson = _find_lesson(course, lesson_id)
    _require_generated_content(lesson)

    now = datetime.now(timezone.utc)

    lesson_progress_collection.update_one(
        {
            "owner_id": user["user_id"],
            "course_id": str(course["_id"]),
            "lesson_id": lesson_id,
        },
        {
            "$set": {
                "completed": request.completed,
                "completed_at": now,
            }
        },
        upsert=True,
    )

    return {
        "lesson_id": lesson_id,
        "completed": request.completed,
        "completed_at": now,
    }


@router.post("/{course_id}/lessons/{lesson_id}/practice")
def check_practice_answers(
    course_id: str,
    lesson_id: str,
    request: PracticeAnswerRequest,
    user=Depends(authenticate),
):
    course = _get_owned_course(course_id, user["user_id"])
    _, lesson = _find_lesson(course, lesson_id)
    content = _require_generated_content(lesson)

    if len(request.answers) != len(content.practice_questions):
        raise HTTPException(
            status_code=400,
            detail="Answer count does not match practice question count",
        )

    results = []

    for question, answer in zip(
        content.practice_questions,
        request.answers,
    ):
        is_correct = (
            " ".join(answer.casefold().split())
            == " ".join(question.expected_answer.casefold().split())
        )

        results.append({
            "is_correct": is_correct,
            "expected_answer": question.expected_answer,
            "feedback": (
                "Correct"
                if is_correct
                else "Review the expected answer and try the next question"
            ),
        })

    return {"results": results}


@router.post("/{course_id}/lessons/{lesson_id}/quiz")
def grade_course_quiz(
    course_id: str,
    lesson_id: str,
    request: QuizAnswerRequest,
    user=Depends(authenticate),
):
    course = _get_owned_course(course_id, user["user_id"])
    _, lesson = _find_lesson(course, lesson_id)
    content = _require_generated_content(lesson)

    if len(request.answers) != len(content.quiz):
        raise HTTPException(
            status_code=400,
            detail="Answer count does not match quiz question count",
        )

    if any(
        answer < 0 or answer >= len(question.options)
        for answer, question in zip(request.answers, content.quiz)
    ):
        raise HTTPException(
            status_code=400,
            detail="Quiz answers must select an available option",
        )

    results = []
    score = 0

    for question, answer in zip(content.quiz, request.answers):
        correct = question.correct_option_index
        is_correct = answer == correct
        score += int(is_correct)

        results.append({
            "question": question.question,
            "selected_answer": question.options[answer],
            "correct_answer": question.options[correct],
            "is_correct": is_correct,
            "feedback": question.explanation,
        })

    now = datetime.now(timezone.utc)

    attempt = {
        "owner_id": user["user_id"],
        "course_id": str(course["_id"]),
        "lesson_id": lesson_id,
        "score": score,
        "total": len(content.quiz),
        "answers": request.answers,
        "results": results,
        "created_at": now,
    }

    course_quiz_history.insert_one(attempt)

    return {
        "score": score,
        "total": len(content.quiz),
        "results": results,
        "created_at": now,
    }
