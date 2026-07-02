import uuid

import pytest


def pytest_configure(config):
    from django.conf import settings

    if not settings.configured:
        settings.configure(
            SECRET_KEY="test-secret-key-not-for-production",
            INSTALLED_APPS=[
                "django.contrib.contenttypes",
                "django.contrib.auth",
                "django.contrib.sessions",
                "django.contrib.messages",
                "django.contrib.admin",
                "stapel_core.django.users",
                "rest_framework",
                "stapel_workspaces",
            ],
            AUTH_USER_MODEL="users.User",
            DATABASES={
                "default": {
                    "ENGINE": "django.db.backends.sqlite3",
                    "NAME": ":memory:",
                }
            },
            DEFAULT_AUTO_FIELD="django.db.models.BigAutoField",
            USE_TZ=True,
            APPEND_SLASH=False,
            ROOT_URLCONF="stapel_workspaces.conftest_urls",
            CACHES={
                "default": {
                    "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
                }
            },
            # In-memory bus — no Kafka/Redis broker needed
            STAPEL_BUS_BACKEND="stapel_core.bus.backends.memory.MemoryBus",
            # Synchronous in-process comm: no outbox table needed, emitted
            # actions reach in-process subscribers immediately.
            STAPEL_COMM={
                "ACTION_TRANSPORT": "inprocess",
                "FUNCTION_TRANSPORT": "inprocess",
                "OUTBOX_ENABLED": False,
                "VALIDATE_SCHEMAS": True,
            },
            SERVICE_NAME="workspaces",
            FRONTEND_URL="https://app.example.com",
            # Skip migrations — create tables directly from models
            MIGRATION_MODULES={
                "users": None,
                "workspaces": None,
            },
        )


@pytest.fixture
def api_client():
    from rest_framework.test import APIClient

    return APIClient()


@pytest.fixture
def user(db):
    from stapel_core.django.users.models import User

    return User.objects.create_user(
        username=f"u-{uuid.uuid4().hex[:8]}",
        email=f"{uuid.uuid4().hex[:8]}@example.com",
        password="testpass-1234",
    )


@pytest.fixture
def other_user(db):
    from stapel_core.django.users.models import User

    return User.objects.create_user(
        username=f"u-{uuid.uuid4().hex[:8]}",
        email=f"{uuid.uuid4().hex[:8]}@example.com",
        password="testpass-1234",
    )


@pytest.fixture
def authed_client(api_client, user):
    api_client.force_authenticate(user=user)
    return api_client
