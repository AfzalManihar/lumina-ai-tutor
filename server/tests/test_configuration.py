import os
import subprocess
import sys
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = SERVER_DIR.parent


def test_runtime_configuration_requires_mongo_without_echoing_values(monkeypatch):
    sys.path.insert(0, str(SERVER_DIR))
    from config.settings import ConfigurationError, validate_runtime_configuration

    monkeypatch.delenv("MONGO_URI", raising=False)
    with pytest.raises(ConfigurationError) as error:
        validate_runtime_configuration()
    assert "MONGO_URI" in str(error.value)
    assert "mongodb://" not in str(error.value)


def test_safe_diagnostic_never_prints_configured_values():
    env = os.environ.copy()
    env["MONGO_URI"] = "mongodb://not-a-real-secret.example"
    env["GROQ_API_KEY"] = "test-placeholder-value"
    result = subprocess.run(
        [sys.executable, str(SERVER_DIR / "test.py")],
        cwd=REPO_DIR, env=env, text=True, capture_output=True, check=True,
    )
    assert "configured: yes" in result.stdout
    assert "not-a-real-secret" not in result.stdout
    assert "test-placeholder-value" not in result.stdout
