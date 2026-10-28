"""Lazy MongoDB collection handles, so imports do not expose or require secrets."""

from functools import lru_cache

from config.settings import get_setting


@lru_cache(maxsize=1)
def _database():
    from pymongo import MongoClient

    uri = get_setting("MONGO_URI")
    database_name = get_setting("DB_NAME", required=False, default="test_db")
    client = MongoClient(uri, serverSelectionTimeoutMS=5000)
    return client[database_name]


class CollectionProxy:
    def __init__(self, name: str):
        self.name = name

    def __getattr__(self, attribute):
        return getattr(_database()[self.name], attribute)


users_collection = CollectionProxy("users")
chunk_collection = CollectionProxy("text")
chat_history_collection = CollectionProxy("chat_history")
quizzes_collection = CollectionProxy("quizzes")
quiz_history = CollectionProxy("history")
courses_collection = CollectionProxy("courses")
lesson_progress_collection = CollectionProxy("lesson_progress")
course_quiz_history = CollectionProxy("course_quiz_history")


def initialize_indexes():
    """Create non-destructive indexes used for account scoping and history reads."""
    database = _database()
    database.command("ping")
    database["users"].create_index("username", unique=True)
    database["subscriptions"].create_index("user_id", unique=True)
    database["processed_webhook_events"].create_index("received_at", expireAfterSeconds=7776000)
    database["chat_history"].create_index([("user_id", 1), ("timestamp", -1)])
    database["ai_usage"].create_index([("user_id", 1), ("created_at", -1)])
    database["usage_budgets"].create_index([("user_id", 1), ("period_start", 1)], unique=True)
    database["learner_memory"].create_index("user_id", unique=True)
    database["quizzes"].create_index([("user_id", 1), ("timestamp", -1)])
    database["history"].create_index([("user_id", 1), ("timestamp", -1)])
    database["courses"].create_index([("owner_id", 1), ("updated_at", -1)])
    database["lesson_progress"].create_index([("owner_id", 1), ("course_id", 1), ("lesson_id", 1)])
    database["course_quiz_history"].create_index([("owner_id", 1), ("course_id", 1), ("created_at", -1)])

    database["text"].create_index("chunk_id", unique=True)
    database["text"].create_index([("owner_id", 1), ("doc_id", 1), ("page", 1)])

ai_usage_collection = CollectionProxy("ai_usage")
usage_budget_collection = CollectionProxy("usage_budgets")
learner_memory_collection = CollectionProxy("learner_memory")

subscriptions_collection = CollectionProxy("subscriptions")
processed_webhook_events = CollectionProxy("processed_webhook_events")
