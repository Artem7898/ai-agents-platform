"""Standalone execution API with the same identity and policy as the hybrid."""

import os

import django


def build_application():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")
    django.setup()
    from src.api.fastapi.application import create_application
    from src.api.security import secure_application

    return secure_application(create_application())


app = build_application()
