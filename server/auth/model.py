import re
from pydantic import BaseModel, ConfigDict, Field, field_validator


class SignupModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, str_strip_whitespace=True)

    fullname: str = Field(min_length=1, max_length=120, alias="fullName")
    email: str = Field(min_length=3, max_length=254)
    username: str = Field(min_length=3, max_length=64, alias="userName")
    password: str = Field(min_length=8, max_length=72)
    school: str | None = Field(default=None, max_length=160)

    @field_validator("password")
    @classmethod
    def bcrypt_byte_limit(cls, value: str) -> str:
        if len(value.encode("utf-8")) > 72:
            raise ValueError("password must be at most 72 UTF-8 bytes")
        return value

    @field_validator("email")
    @classmethod
    def valid_email_shape(cls, value: str) -> str:
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value):
            raise ValueError("email must be a valid email address")
        return value


class StudentUser(SignupModel):
    grade: int = Field(ge=1, le=12)


class TeacherUser(SignupModel):
    pass
