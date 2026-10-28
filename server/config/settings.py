"""Environment configuration helpers. Secret values are never included in errors."""

import os
from dotenv import load_dotenv

load_dotenv()


class ConfigurationError(RuntimeError):
    pass


def get_setting(name: str, *, required: bool = True, default: str | None = None) -> str | None:
    value = os.getenv(name, default)
    if required and (value is None or not value.strip()):
        raise ConfigurationError(f"Required environment variable {name} is not configured")
    return value


def validate_runtime_configuration() -> None:
    get_setting("MONGO_URI")


def require_ai_key(name: str) -> str:
    return get_setting(name)
