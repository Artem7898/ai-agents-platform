#!/usr/bin/env python
"""
Django's command-line utility for administrative tasks.
Customized for AI Agents Platform (Django 5 + Django Nova + DDD).

WHY THIS FILE IS MODIFIED:
1. sys.path injection: Ensures `from src.*` imports work in terminal/CI,
   not just in PyCharm (where it's solved via "Mark Directory as Sources Root").
2. Explicit default settings: Points to `config.settings.dev` for local dev.
3. Strict typing: Uses PEP 484 type hints for the main function.
"""
import os
import sys
from pathlib import Path

# ==============================================================================
# 1. PATH CONFIGURATION (Crucial for DDD 'src/' layout)
# ==============================================================================
# We add the project root to sys.path. This allows Python to resolve imports
# like `from src.agents.models import Agent` when running `python manage.py ...`
# from the terminal, without relying on IDE-specific PYTHONPATH settings.
BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))


# ==============================================================================
# 2. ENVIRONMENT & SETTINGS INITIALIZATION
# ==============================================================================
def main() -> None:
    """Run administrative tasks."""

    # Default to development settings.
    # In production (Docker/CI), this MUST be overridden via environment variable:
    # DJANGO_SETTINGS_MODULE=config.settings.prod
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")

    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "Couldn't import Django. Are you sure it's installed and "
            "available on your PYTHONPATH environment variable? Did you "
            "forget to activate a virtual environment?\n"
            "Hint: Run `source .venv/bin/activate` and `uv pip install -e .[dev]`"
        ) from exc

    # ==============================================================================
    # 3. PRE-EXECUTION HOOKS (Optional)
    # ==============================================================================
    # If you have custom management commands that require OpenTelemetry or
    # structlog to be initialized BEFORE Django's internal setup(), you can
    # trigger it here. For standard commands (migrate, shell), Django handles it.

    # Example:
    # if "run_custom_llm_job" in sys.argv:
    #     from src.core.otel import init_tracing
    #     init_tracing()

    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()