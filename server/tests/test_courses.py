from pathlib import Path
import sys

SERVER_DIR = Path(__file__).resolve().parents[1]
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from courses import generation, routes
from courses.schemas import (
    CourseCreateRequest, CurriculumOutput, LessonOutput, LessonProgressRequest,
    PracticeAnswerRequest, QuizAnswerRequest, QuizQuestion,
)


CURRICULUM = CurriculumOutput.model_validate({
    "objectives": ["Write a basic Python function", "Test a function"],
    "modules": [{
        "title": "First steps", "summary": "Learn function basics.",
        "lessons": [{"title": "Functions", "summary": "Define and call a function."}],
    }],
})
LESSON = LessonOutput.model_validate({
    "explanation": "A function is reusable code.",
    "worked_examples": [{
        "title": "Add two numbers", "problem": "Add 2 and 3.",
        "steps": ["Call the function with 2 and 3."], "answer": "5",
    }],
    "practice_questions": [{
        "question": "What is 4 plus 4?", "hint": "Add both values.",
        "expected_answer": "8",
    }],
    "quiz": [{
        "question": "Which keyword defines a function?", "options": ["def", "for"],
        "correct_option_index": 0, "explanation": "Python uses def.",
    }],
})


def test_course_request_accepts_any_topic_and_rejects_bad_level():
    request = CourseCreateRequest(topic="OpenCV", level="Beginner")
    assert request.topic == "OpenCV"
    with pytest.raises(ValidationError):
        CourseCreateRequest(topic="OpenCV", level="Expert")
    with pytest.raises(ValidationError):
        CourseCreateRequest(topic="  ", level="Beginner")


def test_quiz_correct_answer_must_reference_an_option():
    with pytest.raises(ValidationError):
        QuizQuestion(question="Q", options=["A", "B"], correct_option_index=2, explanation="Because")


def test_structured_generation_uses_injected_model_without_api_key(monkeypatch):
    prompts = []

    class FakeStructuredModel:
        def invoke(self, prompt):
            prompts.append(prompt)
            return CURRICULUM

    monkeypatch.setattr(generation, "_structured_model", lambda schema: FakeStructuredModel())
    result = generation.generate_curriculum(
        topic="OpenCV", level="Beginner", learning_goal="Detect objects", context=""
    )
    assert result.objectives == CURRICULUM.objectives
    assert "OpenCV" in prompts[0]
    assert "Detect objects" in prompts[0]
    assert "None" in prompts[0]


def test_generated_lesson_response_hides_answer_keys():
    lesson = {"lesson_id": "l1", "title": "Functions", "summary": "Basics.", "content": LESSON.model_dump()}
    response = routes._public_lesson({"topic": "Python"}, lesson, "Getting started")
    payload = response.model_dump()
    assert payload["content"]["quiz"][0]["options"] == ["def", "for"]
    assert "correct_option_index" not in str(payload)
    assert "expected_answer" not in str(payload)


def test_course_creation_persists_owner_and_returns_no_owner_field(monkeypatch):
    class FakeCollection:
        document = None

        def insert_one(self, document):
            self.document = document
            return type("Result", (), {"inserted_id": "generated-course-id"})()

    collection = FakeCollection()
    monkeypatch.setattr(routes, "courses_collection", collection)
    monkeypatch.setattr(routes, "generate_curriculum", lambda **kwargs: CURRICULUM)
    result = routes.create_course(
        CourseCreateRequest(topic="Python", level="Beginner"),
        {"user_id": "owner-123"},
    )
    assert collection.document["owner_id"] == "owner-123"
    assert result["course_id"] == "generated-course-id"
    assert "owner_id" not in result


def test_owned_course_query_scopes_by_authenticated_owner(monkeypatch):
    class FakeCollection:
        query = None

        def find_one(self, query):
            self.query = query
            return {"_id": "oid", "owner_id": "owner-123"}

    collection = FakeCollection()
    monkeypatch.setattr(routes, "courses_collection", collection)
    monkeypatch.setattr(routes, "_object_id", lambda value: f"parsed:{value}")
    routes._get_owned_course("course-id", "owner-123")
    assert collection.query == {"_id": "parsed:course-id", "owner_id": "owner-123"}


def test_quiz_grade_is_persisted_with_owner_and_feedback(monkeypatch):
    class FakeHistory:
        document = None

        def insert_one(self, document):
            self.document = document

    history = FakeHistory()
    course = {"_id": "course-id", "modules": [{"title": "Basics", "lessons": [
        {"lesson_id": "l1", "title": "Functions", "summary": "Basics", "content": LESSON.model_dump()}
    ]}]}
    monkeypatch.setattr(routes, "course_quiz_history", history)
    monkeypatch.setattr(routes, "_get_owned_course", lambda course_id, user_id: course)
    result = routes.grade_course_quiz(
        "course-id", "l1", QuizAnswerRequest(answers=[0]), {"user_id": "owner-123"}
    )
    assert result["score"] == 1
    assert result["results"][0]["is_correct"] is True
    assert history.document["owner_id"] == "owner-123"


def test_practice_answer_feedback_uses_saved_answer_key(monkeypatch):
    course = {"_id": "course-id", "modules": [{"title": "Basics", "lessons": [
        {"lesson_id": "l1", "content": LESSON.model_dump()}
    ]}]}
    monkeypatch.setattr(routes, "_get_owned_course", lambda course_id, user_id: course)
    result = routes.check_practice_answers(
        "course-id", "l1", PracticeAnswerRequest(answers=["8"]), {"user_id": "owner-123"}
    )
    assert result["results"][0]["is_correct"] is True



def test_progress_write_is_scoped_to_owner_and_course(monkeypatch):
    class FakeCollection:
        query = None
        update = None

        def update_one(self, query, update, upsert):
            self.query = query
            self.update = update
            self.upsert = upsert

    collection = FakeCollection()
    course = {"_id": "course-id", "modules": [{"lessons": [{"lesson_id": "l1", "content": LESSON.model_dump()}]}]}
    monkeypatch.setattr(routes, "lesson_progress_collection", collection)
    monkeypatch.setattr(routes, "_get_owned_course", lambda course_id, user_id: course)
    result = routes.set_lesson_progress(
        "course-id", "l1", LessonProgressRequest(completed=True), {"user_id": "owner-123"}
    )
    assert collection.query == {
        "owner_id": "owner-123", "course_id": "course-id", "lesson_id": "l1"
    }
    assert collection.upsert is True
    assert result["completed"] is True


def test_invalid_quiz_option_is_rejected(monkeypatch):
    course = {"_id": "course-id", "modules": [{"title": "Basics", "lessons": [
        {"lesson_id": "l1", "content": LESSON.model_dump()}
    ]}]}
    monkeypatch.setattr(routes, "_get_owned_course", lambda course_id, user_id: course)
    with pytest.raises(HTTPException) as error:
        routes.grade_course_quiz(
            "course-id", "l1", QuizAnswerRequest(answers=[4]), {"user_id": "owner-123"}
        )
    assert error.value.status_code == 400

