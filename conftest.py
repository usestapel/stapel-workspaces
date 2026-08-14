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


@pytest.fixture(autouse=True)
def billing_seam():
    """Answer ``billing.check_entitlement`` with "yes" for the whole suite.

    The seam now fails closed, so a suite with no billing at all would get
    503 on every org creation, invitation and provisioning — which would
    say nothing about the workspace logic each of those tests is actually
    about. Standing in a billing that allows keeps them exercising the
    SHIPPED default (``ALLOW_UNBILLED`` stays False) rather than switching
    the safety off. The closed path has its own tests, and any test that
    wants a different verdict registers its own provider (``fake_billing``),
    which replaces this one for its duration.
    """
    from stapel_core.comm.registry import function_registry

    from stapel_workspaces import entitlements

    name = entitlements.CHECK_ENTITLEMENT
    function_registry.register(name, lambda payload: {"allowed": True})
    yield
    function_registry._providers.pop(name, None)
    function_registry._schemas.pop(name, None)


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
