"""Validated request and response contracts for Lumina tutor APIs."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator

class TutorMessage(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=1500)

class TutorChatRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    query: str = Field(min_length=1, max_length=3000)
    level: Literal["Beginner", "Intermediate", "Advanced"] = "Beginner"
    history: list[TutorMessage] = Field(default_factory=list, max_length=10)
    ground_in_documents: bool = False
    remember_memory: bool = False

    @model_validator(mode="after")
    def nonblank_query(self):
        if not self.query.strip():
            raise ValueError("query cannot be blank")
        return self

class TutorQuizRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    topic: str = Field(min_length=2, max_length=200)
    level: Literal["Beginner", "Intermediate", "Advanced"] = "Beginner"
    num_questions: int = Field(default=3, ge=1, le=10)

class QuizQuestion(BaseModel):
    question: str = Field(min_length=3, max_length=600)
    options: list[str] = Field(min_length=2, max_length=4)
    correct_option_index: int = Field(ge=0, le=3)
    explanation: str = Field(min_length=1, max_length=800)
    hint: str = Field(default="Think about the key idea in the question.", max_length=300)

    @model_validator(mode="after")
    def answer_index_exists(self):
        if self.correct_option_index >= len(self.options):
            raise ValueError("correct option must be present")
        return self

class GeneratedQuiz(BaseModel):
    questions: list[QuizQuestion] = Field(min_length=1, max_length=10)

class TutorQuizAnswers(BaseModel):
    answers: list[int] = Field(min_length=1, max_length=10)


