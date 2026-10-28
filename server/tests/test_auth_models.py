import pytest
from pydantic import ValidationError

from server.auth.model import StudentUser, TeacherUser


def test_student_signup_accepts_streamlit_camel_case_payload():
    user = StudentUser.model_validate({
        "fullName": "A Learner",
        "email": "learner@example.com",
        "userName": "learner1",
        "password": "a-long-password",
        "grade": 8,
        "school": "Example School",
    })
    assert user.fullname == "A Learner"
    assert user.username == "learner1"


def test_teacher_signup_does_not_require_student_school_or_grade():
    user = TeacherUser.model_validate({
        "fullName": "A Teacher",
        "email": "teacher@example.com",
        "userName": "teacher1",
        "password": "a-long-password",
    })
    assert user.username == "teacher1"
    assert user.school is None


@pytest.mark.parametrize("grade", [0, 13])
def test_student_grade_out_of_range_is_rejected(grade):
    with pytest.raises(ValidationError):
        StudentUser.model_validate({
            "fullName": "A Learner",
            "email": "learner@example.com",
            "userName": "learner1",
            "password": "a-long-password",
            "grade": grade,
        })


def test_short_signup_password_is_rejected():
    with pytest.raises(ValidationError):
        TeacherUser.model_validate({
            "fullName": "A Teacher",
            "email": "teacher@example.com",
            "userName": "teacher1",
            "password": "short",
        })


def test_bcrypt_password_utf8_byte_limit_is_validated():
    with pytest.raises(ValidationError):
        TeacherUser.model_validate({
            "fullName": "A Teacher",
            "email": "teacher@example.com",
            "userName": "teacher1",
            "password": "é" * 40,
        })
