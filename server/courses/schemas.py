"""Step 1 structured course request and generated-content contracts."""

from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Level = Literal["Beginner", "Intermediate", "Advanced"]


class CourseCreateRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    topic: str = Field(min_length=2, max_length=200)
    level: Level
    learning_goal: str = Field(default="", max_length=500)
    source_id: str | None = Field(default=None, max_length=80)

    @field_validator("topic")
    @classmethod
    def nonblank_topic(cls, value: str) -> str:
        if len(value) < 2:
            raise ValueError("topic must contain at least two characters")
        return value


class LessonOutline(BaseModel):
    title: str = Field(min_length=2, max_length=160)
    summary: str = Field(min_length=1, max_length=500)


class ModuleOutline(BaseModel):
    title: str = Field(min_length=2, max_length=160)
    summary: str = Field(min_length=1, max_length=500)
    lessons: list[LessonOutline] = Field(min_length=1, max_length=8)


class CurriculumOutput(BaseModel):
    objectives: list[str] = Field(min_length=2, max_length=10)
    modules: list[ModuleOutline] = Field(min_length=1, max_length=8)


class WorkedExample(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    problem: str = Field(min_length=1, max_length=1000)
    steps: list[str] = Field(min_length=1, max_length=12)
    answer: str = Field(min_length=1, max_length=2000)


class PracticeQuestion(BaseModel):
    question: str = Field(min_length=1, max_length=1000)
    hint: str = Field(default="", max_length=500)
    expected_answer: str = Field(min_length=1, max_length=2000)


class QuizQuestion(BaseModel):
    question: str = Field(min_length=1, max_length=1000)
    options: list[str] = Field(min_length=2, max_length=6)
    correct_option_index: int = Field(ge=0, le=5)
    explanation: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def correct_option_is_present(self):
        if self.correct_option_index >= len(self.options):
            raise ValueError("correct_option_index must refer to an available option")
        return self


class LessonOutput(BaseModel):
    explanation: str = Field(min_length=1, max_length=10000)
    worked_examples: list[WorkedExample] = Field(min_length=1, max_length=5)
    practice_questions: list[PracticeQuestion] = Field(min_length=1, max_length=10)
    quiz: list[QuizQuestion] = Field(min_length=1, max_length=10)


class QuizAnswerRequest(BaseModel):
    answers: list[int] = Field(min_length=1, max_length=10)


class PracticeAnswerRequest(BaseModel):
    answers: list[Annotated[str, Field(max_length=4000)]] = Field(min_length=1, max_length=10)


class LessonProgressRequest(BaseModel):
    completed: bool = True


class PublicPracticeQuestion(BaseModel):
    question: str
    hint: str


class PublicQuizQuestion(BaseModel):
    question: str
    options: list[str]


class PublicLessonOutput(BaseModel):
    explanation: str
    worked_examples: list[WorkedExample]
    practice_questions: list[PublicPracticeQuestion]
    quiz: list[PublicQuizQuestion]


class LessonResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lesson_id: str
    module_title: str
    title: str
    summary: str
    content: PublicLessonOutput


