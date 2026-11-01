"""Configuration checks run in subprocesses to avoid changing Django settings."""

import json
import os
import subprocess
import sys

import pytest


def production_import(**overrides):
    env = os.environ.copy()
    env.update(
        {
            "SECRET_KEY": "test-only-key-with-more-than-fifty-characters-not-for-real-deployment",
            "DATABASE_URL": "postgresql://audit@localhost/audit",
            "ALLOWED_HOSTS": '["api.example.test"]',
            "SECURE_SSL_REDIRECT": "true",
        }
    )
    env.update(overrides)
    return subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json; import config.settings.prod as s; "
                "print(json.dumps({'debug':s.DEBUG,'secure_cookie':s.SESSION_COOKIE_SECURE,"
                "'ssl_redirect':s.SECURE_SSL_REDIRECT,'conn_max_age':"
                "s.DATABASES['default']['CONN_MAX_AGE']}))"
            ),
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize(
    "overrides,reason",
    [
        ({"SECRET_KEY": ""}, "SECRET_KEY"),
        ({"SECRET_KEY": "django-insecure-" + "x" * 60}, "SECRET_KEY"),
        ({"DATABASE_URL": "sqlite:///db.sqlite3"}, "DATABASE_URL"),
        ({"ALLOWED_HOSTS": '["*"]'}, "ALLOWED_HOSTS"),
        ({"ALLOWED_HOSTS": "[]"}, "ALLOWED_HOSTS"),
    ],
)
def test_production_rejects_unsafe_configuration(overrides, reason):
    result = production_import(**overrides)
    assert result.returncode != 0
    assert reason in result.stderr


def test_production_configuration_enforces_secure_defaults():
    result = production_import()
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "debug": False,
        "secure_cookie": True,
        "ssl_redirect": True,
        "conn_max_age": 0,
    }
