import uuid

import pytest


def pytest_configure(config):
    from django.conf import settings

    if not settings.configured:
        # Single source of truth for this block lives in _codegen_settings.py so
        # the test harness and the contract-emission harness (make contract) can
        # never drift (contract-pipeline.md §3). Tests keep the historical
        # double-mount urlconf + no REST_FRAMEWORK override, exactly as before
        # the extraction.
        from stapel_workspaces._codegen_settings import settings_kwargs

        settings.configure(**settings_kwargs())


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
