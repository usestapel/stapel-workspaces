"""Tests for the internal service-to-service endpoints (X-API-KEY / staff)."""

import uuid

import pytest
from django.test import override_settings

from stapel_workspaces.models import Role, Workspace, WorkspaceType

BASE = "/workspaces/api/workspaces"
SERVICE_KEY = "test-service-key"

service_settings = override_settings(
    MIDDLEWARE=["stapel_core.django.jwt.middleware.ServiceAPIKeyMiddleware"],
    SERVICE_API_KEY=SERVICE_KEY,
)


def _create_ws(user, name="Acme"):
    from stapel_workspaces.services import create_workspace

    return create_workspace(user=user, name=name)


@pytest.mark.django_db
class TestInternalMembership:
    @service_settings
    def test_valid_api_key_gets_membership(self, api_client, user):
        ws = _create_ws(user)
        resp = api_client.get(
            f"{BASE}/internal/{ws.id}/members/{user.id}",
            HTTP_X_API_KEY=SERVICE_KEY,
        )
        assert resp.status_code == 200, resp.content
        data = resp.json()
        assert data["user_id"] == str(user.id)
        assert data["role"] == Role.OWNER

    @service_settings
    def test_wrong_api_key_denied(self, api_client, user):
        ws = _create_ws(user)
        resp = api_client.get(
            f"{BASE}/internal/{ws.id}/members/{user.id}",
            HTTP_X_API_KEY="wrong-key",
        )
        assert resp.status_code in (401, 403)

    def test_no_credentials_denied(self, api_client, user):
        ws = _create_ws(user)
        resp = api_client.get(f"{BASE}/internal/{ws.id}/members/{user.id}")
        assert resp.status_code in (401, 403)

    def test_plain_user_denied(self, authed_client, user):
        ws = _create_ws(user)
        resp = authed_client.get(f"{BASE}/internal/{ws.id}/members/{user.id}")
        assert resp.status_code in (401, 403)

    def test_staff_user_allowed(self, api_client, user, other_user):
        ws = _create_ws(user)
        other_user.is_staff = True
        other_user.save(update_fields=["is_staff"])
        api_client.force_authenticate(user=other_user)
        resp = api_client.get(f"{BASE}/internal/{ws.id}/members/{user.id}")
        assert resp.status_code == 200

    @service_settings
    def test_missing_membership_404(self, api_client, user, other_user):
        ws = _create_ws(user)
        resp = api_client.get(
            f"{BASE}/internal/{ws.id}/members/{other_user.id}",
            HTTP_X_API_KEY=SERVICE_KEY,
        )
        assert resp.status_code == 404


@pytest.mark.django_db
class TestInternalPersonalWorkspace:
    @service_settings
    def test_creates_personal_workspace(self, api_client, user):
        resp = api_client.post(
            f"{BASE}/internal/users/{user.id}/personal",
            HTTP_X_API_KEY=SERVICE_KEY,
        )
        assert resp.status_code == 200, resp.content
        ws_id = resp.json()["workspace_id"]
        ws = Workspace.objects.get(id=ws_id)
        assert ws.type == WorkspaceType.PERSONAL
        assert ws.owner_id == user.id

    @service_settings
    def test_idempotent_get_or_create(self, api_client, user):
        first = api_client.post(
            f"{BASE}/internal/users/{user.id}/personal",
            HTTP_X_API_KEY=SERVICE_KEY,
        )
        second = api_client.post(
            f"{BASE}/internal/users/{user.id}/personal",
            HTTP_X_API_KEY=SERVICE_KEY,
        )
        assert first.json()["workspace_id"] == second.json()["workspace_id"]
        assert (
            Workspace.objects.filter(
                owner=user, type=WorkspaceType.PERSONAL
            ).count()
            == 1
        )

    @service_settings
    def test_unknown_user_404(self, api_client, db):
        resp = api_client.post(
            f"{BASE}/internal/users/{uuid.uuid4()}/personal",
            HTTP_X_API_KEY=SERVICE_KEY,
        )
        assert resp.status_code == 404

    def test_no_credentials_denied(self, api_client, user):
        resp = api_client.post(f"{BASE}/internal/users/{user.id}/personal")
        assert resp.status_code in (401, 403)
