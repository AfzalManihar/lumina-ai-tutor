"""Structured LLM generation for topic-based courses and lessons."""

import os
from config.settings import ConfigurationError, require_ai_key
from courses.schemas import CurriculumOutput, LessonOutput


CURRICULUM_PROMPT = """Create a practical learning curriculum for the learner-selected topic.
Topic: {topic}
Learner level: {level}
Learning goal: {learning_goal}
Source notes (if supplied):
{context}

Return concise observable objectives and an ordered curriculum of modules and lessons.
Do not assume the learner uploaded a document. Use reliable general knowledge when source notes are absent. If notes are supplied, use them for source-specific claims and avoid inventing citations.
"""

LESSON_PROMPT = """Write one structured lesson for a course.
Course topic: {topic}
Learner level: {level}
Learning goal: {learning_goal}
Course objectives: {objectives}
Module: {module_title}
Lesson: {lesson_title}
Lesson summary: {lesson_summary}
Source notes (if supplied):
{context}

Explain simply at the selected level. Include at least one fully worked example before independent practice. Practice questions must follow the worked examples. Include a small multiple-choice quiz. Keep expected practice answers and quiz answer indices in the structured response for server-side grading; never include them in the learner-facing lesson response.
"""


def _structured_model(schema):
    api_key = require_ai_key("GROQ_API_KEY")
    from langchain_groq import ChatGroq

    model_name = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
    llm = ChatGroq(temperature=0.2, model_name=model_name, groq_api_key=api_key)
    return llm.with_structured_output(schema)


def _invoke(schema, prompt: str):
    try:
        result = _structured_model(schema).invoke(prompt)
        return result if isinstance(result, schema) else schema.model_validate(result)
    except ConfigurationError:
        raise
    except Exception as exc:
        raise GenerationError("The AI provider could not generate valid structured content") from exc


class GenerationError(RuntimeError):
    pass


def generate_curriculum(*, topic: str, level: str, learning_goal: str, context: str = "") -> CurriculumOutput:
    prompt = CURRICULUM_PROMPT.format(
        topic=topic, level=level, learning_goal=learning_goal or "Not specified", context=context or "None"
    )
    result = _invoke(CurriculumOutput, prompt)
    return result if isinstance(result, CurriculumOutput) else CurriculumOutput.model_validate(result)


def generate_lesson(*, topic: str, level: str, learning_goal: str, objectives: list[str],
                    module_title: str, lesson_title: str, lesson_summary: str,
                    context: str = "") -> LessonOutput:
    prompt = LESSON_PROMPT.format(
        topic=topic, level=level, learning_goal=learning_goal or "Not specified",
        objectives="; ".join(objectives), module_title=module_title,
        lesson_title=lesson_title, lesson_summary=lesson_summary, context=context or "None",
    )
    result = _invoke(LessonOutput, prompt)
    return result if isinstance(result, LessonOutput) else LessonOutput.model_validate(result)
