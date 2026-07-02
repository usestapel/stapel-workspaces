"""API tests for workspace CRUD edge cases."""

import uuid

import pytest
from django.utils import timezone

from stapel_workspaces.errors import (
    ERR_400_SLUG_TAKEN,
    ERR_403_FORBIDDEN_WORKSPACE,
    ERR_404_WORKSPACE_NOT_FOUND,
)
from stapel_workspaces.models import Role, Workspace, WorkspaceMember

BASE = "/workspaces/api/workspaces"


def _create_ws(user, name="Acme", **kwargs):
    from stapel_workspaces.services import create_workspace

    return create_workspace(user=user, name=name, **kwargs)


def _add_member(ws, user, role):
    return WorkspaceMember.objects.create(
        workspace=ws, user=user, role=role, accepted_at=timezone.now()
    )


@pytest.mark.django_db
class TestWorkspaceListCreate:
    def test_list_requires_auth(self, api_client):
        assert api_client.get(BASE).status_code in (401, 403)

    def test_create_requires_auth(self, api_client):
        resp = api_client.post(BASE, {"name": "X"}, format="json")
        assert resp.status_code in (401, 403)

    def test_list_excludes_soft_deleted(self, authed_client, user):
        alive = _create_ws(user, name="Alive")
        dead = _create_ws(user, name="Dead")
        dead.deleted_at = timezone.now()
        dead.save(update_fields=["deleted_at"])

        resp = authed_client.get(BASE)
        assert resp.status_code == 200
        ids = [w["id"] for w in resp.json()["workspaces"]]
        assert ids == [str(alive.id)]

    def test_create_invalid_type_rejected(self, authed_client):
        resp = authed_client.post(
            BASE, {"name": "X", "type": "galactic"}, format="json"
        )
        assert resp.status_code == 400
        assert Workspace.objects.count() == 0

    def test_create_missing_name_rejected(self, authed_client):
        resp = authed_client.post(BASE, {}, format="json")
        assert resp.status_code == 400

    def test_create_autogenerates_unique_slug(self, authed_client, user):
        _create_ws(user, name="Acme", slug="acme")
        resp = authed_client.post(BASE, {"name": "Acme"}, format="json")
        assert resp.status_code == 201
        assert resp.json()["slug"] == "acme-2"


@pytest.mark.django_db
class TestWorkspaceDetail:
    def test_get_requires_auth(self, api_client, user):
        ws = _create_ws(user)
        assert api_client.get(f"{BASE}/{ws.id}").status_code in (401, 403)

    def test_get_returns_workspace_and_touches_access_time(
        self, authed_client, user
    ):
        ws = _create_ws(user)
        member = ws.members.get(user=user)
        assert member.last_accessed_at is None

        resp = authed_client.get(f"{BASE}/{ws.id}")
        assert resp.status_code == 200, resp.content
        data = resp.json()
        assert data["id"] == str(ws.id)
        assert data["my_role"] == Role.OWNER
        assert data["member_count"] == 1
        member.refresh_from_db()
        assert member.last_accessed_at is not None

    def test_get_unknown_404(self, authed_client):
        resp = authed_client.get(f"{BASE}/{uuid.uuid4()}")
        assert resp.status_code == 404
        assert resp.json()["localizable_error"] == ERR_404_WORKSPACE_NOT_FOUND

    def test_get_soft_deleted_404(self, authed_client, user):
        ws = _create_ws(user)
        ws.deleted_at = timezone.now()
        ws.save(update_fields=["deleted_at"])
        resp = authed_client.get(f"{BASE}/{ws.id}")
        assert resp.status_code == 404

    def test_patch_updates_name_and_settings(self, authed_client, user):
        ws = _create_ws(user)
        resp = authed_client.patch(
            f"{BASE}/{ws.id}",
            {"name": "Renamed", "settings": {"color": "teal"}},
            format="json",
        )
        assert resp.status_code == 200, resp.content
        assert resp.json()["name"] == "Renamed"
        ws.refresh_from_db()
        assert ws.name == "Renamed"
        assert ws.settings == {"color": "teal"}

    def test_patch_slug_conflict_400(self, authed_client, user):
        _create_ws(user, name="Other", slug="taken")
        ws = _create_ws(user, name="Mine", slug="mine")
        resp = authed_client.patch(
            f"{BASE}/{ws.id}", {"slug": "taken"}, format="json"
        )
        assert resp.status_code == 400
        assert resp.json()["localizable_error"] == ERR_400_SLUG_TAKEN
        ws.refresh_from_db()
        assert ws.slug == "mine"

    def test_patch_to_new_slug_succeeds(self, authed_client, user):
        ws = _create_ws(user, name="Mine", slug="mine")
        resp = authed_client.patch(
            f"{BASE}/{ws.id}", {"slug": "fresh"}, format="json"
        )
        assert resp.status_code == 200
        ws.refresh_from_db()
        assert ws.slug == "fresh"

    def test_patch_by_plain_member_403(self, api_client, user, other_user):
        ws = _create_ws(other_user)
        _add_member(ws, user, Role.MEMBER)
        api_client.force_authenticate(user=user)
        resp = api_client.patch(f"{BASE}/{ws.id}", {"name": "Nope"}, format="json")
        assert resp.status_code == 403
        assert resp.json()["localizable_error"] == ERR_403_FORBIDDEN_WORKSPACE
        ws.refresh_from_db()
        assert ws.name != "Nope"

    def test_patch_unknown_workspace_404(self, authed_client):
        resp = authed_client.patch(
            f"{BASE}/{uuid.uuid4()}", {"name": "X"}, format="json"
        )
        assert resp.status_code == 404

    def test_delete_unknown_workspace_404(self, authed_client):
        resp = authed_client.delete(f"{BASE}/{uuid.uuid4()}")
        assert resp.status_code == 404

    def test_delete_by_admin_403(self, api_client, user, other_user):
        ws = _create_ws(other_user)
        _add_member(ws, user, Role.ADMIN)
        api_client.force_authenticate(user=user)
        resp = api_client.delete(f"{BASE}/{ws.id}")
        assert resp.status_code == 403
        ws.refresh_from_db()
        assert ws.deleted_at is None
