import os
import streamlit as st
import requests
from requests.auth import HTTPBasicAuth
from io import BytesIO
import re
from dotenv import load_dotenv


load_dotenv()

# CONFIG

BACKEND_URL = os.getenv("BACKEND_URL") or "http://127.0.0.1:8000"

BASE_DIR = os.path.dirname(__file__)
ASSETS_DIR = os.path.join(BASE_DIR, "assets")

st.set_page_config(
    page_title="TutorRAG",
    page_icon="🎓",
    layout="wide",
)


# SESSION STATE INIT

def init_state():
    defaults = {
        "page": "landing",
        "authenticated": False,
        "username": "",
        "password": "",
        "role": "",
        "grade": 0,
        "chat_messages": [],
        "generated_quiz": None,
        "quiz_result": None,
        "quiz_history": None,
        "courses": [],
        "active_course": None,
        "active_lesson": None,
        "course_source_id": None,
        "course_quiz_result": None,
        "practice_result": None,
    }
    for k, v in defaults.items():
        st.session_state.setdefault(k, v)

init_state()


# HELPERS

def auth():
    return HTTPBasicAuth(
        st.session_state.username,
        st.session_state.password,
    )

def api(method, path, **kwargs):
    request_auth = kwargs.pop("auth", auth())
    try:
        return requests.request(
            method,
            f"{BACKEND_URL}{path}",
            auth=request_auth,
            timeout=60,
            **kwargs,
        )
    except requests.RequestException as exc:
        response = requests.Response()
        response.status_code = 503
        response._content = f"Cannot reach the AI Tutor API: {exc}".encode("utf-8")
        return response

def logout():
    for k in list(st.session_state.keys()):
        del st.session_state[k]


# LANDING PAGE

def landing_page():
    col1, col2 = st.columns([1.3, 1])

    with col1:
        st.markdown("## 🎓 TutorRAG")
        st.markdown(
            """
            ### AI-Powered Learning From Your Own Documents

            - Upload textbooks & notes  
            - Ask questions using AI  
            - Generate quizzes automatically  
            - Track learning progress  

            **Built using RAG + LLMs**
            """
        )

        b1, b2 = st.columns(2)
        with b1:
            if st.button("🚀 Get Started", use_container_width=True):
                st.session_state.page = "signup"
        with b2:
            if st.button("🔐 Login", use_container_width=True):
                st.session_state.page = "login"

    with col2:
        img = os.path.join(ASSETS_DIR, "landing-page.jpg")

        if os.path.exists(img):
            st.image(img, width="stretch")


# LOGIN PAGE

def login_page():
    col1, col2 = st.columns([1, 1])

    with col1:
        img = os.path.join(ASSETS_DIR, "login.jpg")

        if os.path.exists(img):
            st.image(img, width="stretch")

    with col2:
        st.markdown("## 🔐 Login")

        with st.form("login_form"):
            username = st.text_input("Username")
            password = st.text_input("Password", type="password")
            submitted = st.form_submit_button("Login")

            if submitted:
                r = api("GET", "/login", auth=HTTPBasicAuth(username, password))
                if r.status_code == 200:
                    data = r.json()
                    st.session_state.update({
                        "authenticated": True,
                        "username": username,
                        "password": password,
                        "role": data["role"],
                        "grade": data.get("grade", 0),
                        "page": "app",
                    })
                    st.success("Login successful")
                else:
                    st.error("Invalid credentials")

        if st.button("⬅ Back"):
            st.session_state.page = "landing"


# SIGNUP PAGE

def signup_page():
    col1, col2 = st.columns([1, 1])

    with col1:
        img = os.path.join(ASSETS_DIR, "signup.png")

        if os.path.exists(img):
            st.image(img, width="stretch")

    with col2:
        st.markdown("## ✍️ Create Account")
        role = st.selectbox("I am a", ["Student", "Teacher"])

        with st.form("signup_form"):
            full_name = st.text_input("Full Name")
            email = st.text_input("Email")
            username = st.text_input("Username")
            password = st.text_input("Password", type="password")

            if role == "Student":
                grade = st.number_input("Grade", 1, 12)
                school = st.text_input("School")

            submitted = st.form_submit_button("Create Account")

            if submitted:
                endpoint = "/signup/student" if role == "Student" else "/signup/teacher"
                payload = {
                    "fullName": full_name,
                    "email": email,
                    "userName": username,
                    "password": password,
                }
                if role == "Student":
                    payload.update({"grade": grade, "school": school})

                r = api("POST", endpoint, json=payload, auth=None)

                if r.status_code == 200:
                    st.success("Account created successfully")
                else:
                    st.error(r.text)

        if st.button("⬅ Back"):
            st.session_state.page = "landing"


# TEACHER DASHBOARD

def teacher_dashboard():
    img = os.path.join(ASSETS_DIR, "teacher.jpg")
    if os.path.exists(img):
        st.image(img, width=220)

    upload_tab, course_tab = st.tabs(["📚 Upload Study Material", "🎓 My Courses"])
    with upload_tab:
        st.markdown("## 📚 Upload Study Material")
        pdf = st.file_uploader("Upload PDF", type="pdf")
        grade = st.number_input("Grade", 1, 12)
        if st.button("Upload Document", disabled=not pdf):
            with st.spinner("Uploading and indexing..."):
                files = {"file": (pdf.name, BytesIO(pdf.getvalue()), "application/pdf")}
                data = {"grade": str(int(grade))}
                response = api("POST", "/upload_docs", files=files, data=data)
            if response.status_code == 200:
                st.success("Document uploaded successfully")
            else:
                st.error(response.text)
    with course_tab:
        course_workspace()

# TOPIC-BASED COURSE WORKSPACE

def course_workspace():
    st.subheader("Create a course from any topic")
    st.caption("Choose a topic and level. A PDF is optional and private to your account.")

    source_file = st.file_uploader("Optional private PDF source", type=["pdf"], key="course_source_file")
    if st.button("Upload PDF source", disabled=source_file is None, key="upload_course_source"):
        with st.spinner("Processing your private source..."):
            response = api("POST", "/courses/sources", files={
                "file": (source_file.name, source_file.getvalue(), "application/pdf")
            })
        if response.status_code == 200:
            st.session_state.course_source_id = response.json()["source_id"]
            st.success("Private source is ready for your course.")
        else:
            st.error(response.text)
    if st.session_state.course_source_id:
        st.caption("An uploaded source will be used for your next course. Clear the file input to omit it.")

    with st.form("create_course_form"):
        topic = st.text_input("Topic", placeholder="Python, OpenCV, Machine Learning, Data Science...")
        level = st.selectbox("Learner level", ["Beginner", "Intermediate", "Advanced"])
        learning_goal = st.text_input("What would you like to be able to do? (optional)")
        create = st.form_submit_button("Generate course", disabled=len(topic.strip()) < 2)
    if create:
        payload = {"topic": topic.strip(), "level": level, "learning_goal": learning_goal.strip()}
        if st.session_state.course_source_id:
            payload["source_id"] = st.session_state.course_source_id
        with st.spinner("Designing your curriculum..."):
            response = api("POST", "/courses", json=payload)
        if response.status_code == 200:
            course = response.json()
            st.session_state.active_course = course
            st.session_state.courses = [
                item for item in st.session_state.courses if item.get("course_id") != course["course_id"]
            ] + [course]
            st.session_state.course_source_id = None
            st.session_state.active_lesson = None
            st.success("Course created and saved to your account.")
        else:
            st.error(response.text)

    left, right = st.columns([1, 2])
    with left:
        if st.button("Refresh my courses"):
            response = api("GET", "/courses")
            if response.status_code == 200:
                st.session_state.courses = response.json()["courses"]
            else:
                st.error(response.text)
        courses = st.session_state.courses
        if courses:
            course_ids = [course["course_id"] for course in courses]
            labels = {course["course_id"]: f"{course['topic']} · {course['level']}" for course in courses}
            selected_id = st.selectbox("Your courses", course_ids, format_func=lambda value: labels[value])
            if st.button("Open course"):
                response = api("GET", f"/courses/{selected_id}")
                if response.status_code == 200:
                    st.session_state.active_course = response.json()
                    st.session_state.active_lesson = None
                else:
                    st.error(response.text)
        else:
            st.info("Create a course or refresh your course list.")

    with right:
        course = st.session_state.active_course
        if not course:
            return
        st.markdown(f"### {course['topic']} · {course['level']}")
        if course.get("learning_goal"):
            st.write(course["learning_goal"])
        st.markdown("**Learning objectives**")
        for objective in course.get("objectives", []):
            st.markdown(f"- {objective}")

        lessons = []
        for module in course.get("modules", []):
            st.markdown(f"#### {module['title']}")
            st.caption(module["summary"])
            for lesson in module.get("lessons", []):
                lessons.append({**lesson, "module_title": module["title"]})
        if not lessons:
            return
        lesson_by_id = {lesson["lesson_id"]: lesson for lesson in lessons}
        completed_ids = {item.get("lesson_id") for item in course.get("progress", []) if item.get("completed")}
        lesson_id = st.selectbox(
            "Lesson", list(lesson_by_id),
            format_func=lambda value: ("✅ " if value in completed_ids else "") + lesson_by_id[value]["title"],
        )
        if st.button("Generate / open lesson"):
            response = api(
                "POST", f"/courses/{course['course_id']}/lessons/{lesson_id}/generate"
            )
            if response.status_code == 200:
                st.session_state.active_lesson = response.json()
                st.session_state.course_quiz_result = None
                st.session_state.practice_result = None
            else:
                st.error(response.text)
        lesson = st.session_state.active_lesson
        if not lesson or lesson.get("lesson_id") != lesson_id:
            return
        st.markdown(f"### {lesson['title']}")
        st.write(lesson["content"]["explanation"])
        for example in lesson["content"]["worked_examples"]:
            with st.expander(f"Worked example: {example['title']}", expanded=True):
                st.markdown(f"**Problem:** {example['problem']}")
                for index, step in enumerate(example["steps"], start=1):
                    st.markdown(f"{index}. {step}")
                st.markdown(f"**Answer:** {example['answer']}")
        with st.form(f"practice_{lesson_id}"):
            st.markdown("#### Practice")
            practice_answers = [
                st.text_input(item["question"], key=f"practice_{lesson_id}_{index}", help=item["hint"])
                for index, item in enumerate(lesson["content"]["practice_questions"])
            ]
            check_practice = st.form_submit_button("Check practice answers")
        if check_practice:
            response = api("POST", f"/courses/{course['course_id']}/lessons/{lesson_id}/practice",
                           json={"answers": practice_answers})
            if response.status_code == 200:
                st.session_state.practice_result = response.json()["results"]
            else:
                st.error(response.text)
        if st.session_state.practice_result:
            for index, result in enumerate(st.session_state.practice_result, start=1):
                st.write(f"Practice {index}: {result['feedback']}")
                if not result["is_correct"]:
                    st.caption(f"Expected answer: {result['expected_answer']}")

        with st.form(f"course_quiz_{lesson_id}"):
            st.markdown("#### Lesson quiz")
            quiz_answers = []
            for index, question in enumerate(lesson["content"]["quiz"]):
                st.markdown(f"**{index + 1}. {question['question']}**")
                quiz_answers.append(st.radio(
                    "Choose an answer", list(range(len(question["options"]))),
                    format_func=lambda choice, options=question["options"]: options[choice],
                    key=f"course_quiz_{lesson_id}_{index}",
                ))
            submit_quiz = st.form_submit_button("Submit quiz")
        if submit_quiz:
            response = api("POST", f"/courses/{course['course_id']}/lessons/{lesson_id}/quiz",
                           json={"answers": quiz_answers})
            if response.status_code == 200:
                st.session_state.course_quiz_result = response.json()
                course_response = api("GET", f"/courses/{course['course_id']}")
                if course_response.status_code == 200:
                    course = course_response.json()
                    st.session_state.active_course = course
            else:
                st.error(response.text)
        if st.session_state.course_quiz_result:
            result = st.session_state.course_quiz_result
            st.success(f"Quiz score: {result['score']}/{result['total']}")
            for item in result["results"]:
                st.markdown(f"{'✅' if item['is_correct'] else '❌'} {item['question']}")
                st.caption(f"Correct answer: {item['correct_answer']}. {item['feedback']}")
        if st.button("Mark lesson complete", key=f"complete_{lesson_id}"):
            response = api("POST", f"/courses/{course['course_id']}/lessons/{lesson_id}/progress",
                           json={"completed": True})
            if response.status_code == 200:
                st.success("Lesson completion saved.")
                course.setdefault("progress", []).append({"lesson_id": lesson_id, "completed": True})
                st.session_state.active_course = course
            else:
                st.error(response.text)
        attempts = course.get("quiz_attempts", [])
        if attempts:
            st.markdown("#### Saved quiz scores")
            for attempt in attempts:
                st.caption(f"{attempt['score']}/{attempt['total']} · {attempt['lesson_id']}")

# STUDENT DASHBOARD

def student_dashboard():
    img = os.path.join(ASSETS_DIR, "students.jpg")

    if os.path.exists(img):
        st.image(img, width=220)

    course_tab, chat_tab, quiz_tab, history_tab = st.tabs(
        ["🎓 Courses", "💬 Ask Questions", "📝 Quiz Generator", "📜 Quiz History"]
    )
    with course_tab:
        course_workspace()

    # ---------------- CHAT ----------------
    with chat_tab:
        for msg in st.session_state.chat_messages:
            with st.chat_message(msg["role"]):
                st.markdown(msg["content"])

        if prompt := st.chat_input("Ask a question"):
            st.session_state.chat_messages.append(
                {"role": "user", "content": prompt}
            )

            with st.chat_message("assistant"):
                with st.spinner("Thinking..."):
                    r = api("POST", "/chat", json={"query": prompt})
                    if r.status_code == 200:
                        data = r.json()
                        answer = data["answer"]
                        if data["sources"]:
                            answer += "\n\n**Sources:** " + ", ".join(data["sources"])
                        st.markdown(answer)
                        st.session_state.chat_messages.append(
                            {"role": "assistant", "content": answer}
                        )
                    else:
                        st.error(r.text)

    # ---------------- QUIZ ----------------
    with quiz_tab:
        if st.session_state.generated_quiz is None:
            topic = st.text_input("Quiz Topic")
            num_q = st.slider("Number of Questions", 1, 10, 3)

            if st.button("Generate Quiz", disabled=not topic):
                r = api(
                    "POST",
                    "/quiz",
                    json={"topic": topic, "num_questions": num_q},
                )
                if r.status_code == 200:
                    st.session_state.generated_quiz = r.json()
                    st.session_state.generated_quiz["topic"] = topic
                    st.session_state.quiz_result = None
                else:
                    st.error(r.text)

        else:
            quiz = st.session_state.generated_quiz
            raw = quiz["quiz"]

            blocks = re.split(r"(Question \d+:)", raw)[1:]
            questions = []

            for i in range(0, len(blocks), 2):
                lines = blocks[i + 1].strip().split("\n")
                q_text = lines[0]
                options = [l for l in lines if re.match(r"[A-Z]\)", l)]
                questions.append({"q": q_text, "opts": options})

            with st.form("quiz_form"):
                answers = []

                for i, q in enumerate(questions):
                    st.markdown(f"**Q{i+1}. {q['q']}**")
                    choice = st.radio(
                        "Choose:",
                        [o[0] for o in q["opts"]],
                        format_func=lambda x: next(
                            o for o in q["opts"] if o.startswith(x)
                        ),
                        key=f"quiz_q{i}",
                    )
                    answers.append(choice)

                submitted = st.form_submit_button("Submit Quiz")

            if submitted:
                r = api(
                    "POST",
                    "/quiz/check",
                    json={
                        "quiz_id": quiz["quiz_id"],
                        "answers": answers,
                    },
                )
                if r.status_code == 200:
                    st.session_state.quiz_result = r.json()
                    st.session_state.generated_quiz = None
                else:
                    st.error(r.text)

        if st.session_state.quiz_result:
            res = st.session_state.quiz_result
            st.success(res["message"])

            for r in res["results"]:
                status = "✅ Correct" if r["is_correct"] else "❌ Incorrect"
                st.markdown(
                    f"**Q{r['question_number']} — {status}**  \n"
                    f"Your Answer: **{r['user_answer']}**  \n"
                    f"Correct Answer: **{r['correct_answer']}**"
                )

            if st.button("Start New Quiz"):
                st.session_state.quiz_result = None

    # ---------------- HISTORY ----------------
    with history_tab:
        if st.button("Load History"):
            r = api("GET", "/quiz/history")
            if r.status_code == 200:
                st.session_state.quiz_history = r.json()["history"]
            else:
                st.error(r.text)

        history = st.session_state.quiz_history
        if history:
            for attempt in history:
                score = attempt["score"]
                total = attempt["total"]
                percent = int((score / total) * 100)

                with st.expander(
                    f"{attempt['topic']} — {score}/{total} ({percent}%)"
                ):
                    raw_quiz = attempt["quiz_content"]

                    blocks = re.split(r"(Question \d+:)", raw_quiz)[1:]
                    parsed = []

                    for i in range(0, len(blocks), 2):
                        lines = blocks[i + 1].strip().split("\n")
                        parsed.append({
                            "question": lines[0],
                            "options": [
                                l for l in lines if re.match(r"[A-Z]\)", l)
                            ],
                        })

                    for i, res in enumerate(attempt["results"]):
                        q = parsed[i]
                        st.markdown(f"### Q{i+1}: {q['question']}")

                        for opt in q["options"]:
                            letter = opt[0]
                            if letter == res["correct_answer"]:
                                st.markdown(f"✅ **{opt}** (Correct)")
                            elif letter == res["user_answer"]:
                                st.markdown(f"❌ **{opt}** (Your Answer)")
                            else:
                                st.markdown(opt)

                        st.divider()


# ROUTER

if st.session_state.page == "landing":
    landing_page()
elif st.session_state.page == "login":
    login_page()
elif st.session_state.page == "signup":
    signup_page()
elif st.session_state.page == "app":
    st.sidebar.title("TutorRAG 🎓")
    st.sidebar.success(
        f"{st.session_state.username} ({st.session_state.role})"
    )
    st.sidebar.button("Logout", on_click=logout)

    if st.session_state.role == "Teacher":
        teacher_dashboard()
    else:
        student_dashboard()
