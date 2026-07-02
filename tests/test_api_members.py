"""API tests for member list / update / removal, incl. owner-escalation guards."""

import uuid

import pytest
from django.utils import timezone

from stapel_workspaces.errors import (
    ERR_400_INVALID_ROLE,
    ERR_403_FORBIDDEN_WORKSPACE,
    ERR_403_LAST_OWNER,
    ERR_404_MEMBER_NOT_FOUND,
)
from stapel_workspaces.models import Role, WorkspaceMember

BASE = "/workspaces/api/workspaces"


@pytest.fixture
def third_user(db):
    from stapel_core.django.users.models import User

    return User.objects.create_user(
        username=f"u-{uuid.uuid4().hex[:8]}",
        email=f"{uuid.uuid4().hex[:8]}@example.com",
        password="testpass-1234",
    )


def _create_ws(user, name="Acme"):
    from stapel_workspaces.services import create_workspace

    return create_workspace(user=user, name=name)


def _add_member(ws, user, role):
    return WorkspaceMember.objects.create(
        workspace=ws, user=user, role=role, accepted_at=timezone.now()
    )


@pytest.mark.django_db
class TestMemberList:
    def test_requires_auth(self, api_client, user):
        ws = _create_ws(user)
        assert api_client.get(f"{BASE}/{ws.id}/members").status_code in (401, 403)

    def test_member_can_list(self, api_client, user, other_user):
        ws = _create_ws(other_user)
        _add_member(ws, user, Role.VIEWER)
        api_client.force_authenticate(user=user)
        resp = api_client.get(f"{BASE}/{ws.id}/members")
        assert resp.status_code == 200, resp.content
        members = resp.json()["members"]
        assert len(members) == 2
        by_user = {m["user_id"]: m for m in members}
        assert by_user[str(other_user.id)]["role"] == Role.OWNER
        assert by_user[str(user.id)]["role"] == Role.VIEWER
        assert by_user[str(user.id)]["email"] == user.email

    def test_non_member_403(self, authed_client, other_user):
        ws = _create_ws(other_user)
        resp = authed_client.get(f"{BASE}/{ws.id}/members")
        assert resp.status_code == 403


@pytest.mark.django_db
class TestMemberUpdate:
    def test_requires_auth(self, api_client, user, other_user):
        ws = _create_ws(user)
        resp = api_client.patch(
            f"{BASE}/{ws.id}/members/{other_user.id}",
            {"role": "admin"},
            format="json",
        )
        assert resp.status_code in (401, 403)

    def test_owner_can_change_role(self, authed_client, user, other_user):
        ws = _create_ws(user)
        member = _add_member(ws, other_user, Role.MEMBER)
        resp = authed_client.patch(
            f"{BASE}/{ws.id}/members/{other_user.id}",
            {"role": "admin"},
            format="json",
        )
        assert resp.status_code == 200, resp.content
        assert resp.json()["role"] == Role.ADMIN
        member.refresh_from_db()
        assert member.role == Role.ADMIN

    def test_admin_cannot_grant_owner(self, api_client, user, other_user):
        """Owner-escalation guard: an admin must not mint owners (incl. self)."""
        ws = _create_ws(other_user)
        me = _add_member(ws, user, Role.ADMIN)
        api_client.force_authenticate(user=user)
        resp = api_client.patch(
            f"{BASE}/{ws.id}/members/{user.id}", {"role": "owner"}, format="json"
        )
        assert resp.status_code == 403
        assert resp.json()["localizable_error"] == ERR_403_FORBIDDEN_WORKSPACE
        me.refresh_from_db()
        assert me.role == Role.ADMIN

    def test_admin_cannot_change_owner_role(self, api_client, user, other_user):
        ws = _create_ws(other_user)
        _add_member(ws, user, Role.ADMIN)
        api_client.force_authenticate(user=user)
        resp = api_client.patch(
            f"{BASE}/{ws.id}/members/{other_user.id}",
            {"role": "member"},
            format="json",
        )
        assert resp.status_code == 403
        assert (
            WorkspaceMember.objects.get(workspace=ws, user=other_user).role
            == Role.OWNER
        )

    def test_sole_owner_cannot_be_demoted(self, authed_client, user):
        ws = _create_ws(user)
        resp = authed_client.patch(
            f"{BASE}/{ws.id}/members/{user.id}", {"role": "admin"}, format="json"
        )
        assert resp.status_code == 403
        assert resp.json()["localizable_error"] == ERR_403_LAST_OWNER

    def test_owner_can_be_demoted_when_another_owner_exists(
        self, authed_client, user, other_user
    ):
        ws = _create_ws(user)
        _add_member(ws, other_user, Role.OWNER)
        resp = authed_client.patch(
            f"{BASE}/{ws.id}/members/{user.id}", {"role": "admin"}, format="json"
        )
        assert resp.status_code == 200
        assert (
            WorkspaceMember.objects.get(workspace=ws, user=user).role == Role.ADMIN
        )

    def test_invalid_role_rejected(self, authed_client, user, other_user):
        ws = _create_ws(user)
        _add_member(ws, other_user, Role.MEMBER)
        resp = authed_client.patch(
            f"{BASE}/{ws.id}/members/{other_user.id}",
            {"role": "supreme-leader"},
            format="json",
        )
        assert resp.status_code == 400
        assert ERR_400_INVALID_ROLE in str(resp.json())

    def test_member_not_found_404(self, authed_client, user, other_user):
        ws = _create_ws(user)
        resp = authed_client.patch(
            f"{BASE}/{ws.id}/members/{other_user.id}",
            {"role": "member"},
            format="json",
        )
        assert resp.status_code == 404
        assert resp.json()["localizable_error"] == ERR_404_MEMBER_NOT_FOUND

    def test_non_admin_cannot_update(self, api_client, user, other_user, third_user):
        ws = _create_ws(other_user)
        _add_member(ws, user, Role.MEMBER)
        _add_member(ws, third_user, Role.MEMBER)
        api_client.force_authenticate(user=user)
        resp = api_client.patch(
            f"{BASE}/{ws.id}/members/{third_user.id}",
            {"role": "viewer"},
            format="json",
        )
        assert resp.status_code == 403


@pytest.mark.django_db
class TestMemberRemove:
    def test_requires_auth(self, api_client, user, other_user):
        ws = _create_ws(user)
        _add_member(ws, other_user, Role.MEMBER)
        resp = api_client.delete(f"{BASE}/{ws.id}/members/{other_user.id}")
        assert resp.status_code in (401, 403)

    def test_admin_can_remove_member(self, api_client, user, other_user, third_user):
        ws = _create_ws(other_user)
        _add_member(ws, user, Role.ADMIN)
        _add_member(ws, third_user, Role.MEMBER)
        api_client.force_authenticate(user=user)
        resp = api_client.delete(f"{BASE}/{ws.id}/members/{third_user.id}")
        assert resp.status_code == 204
        assert not WorkspaceMember.objects.filter(
            workspace=ws, user=third_user
        ).exists()

    def test_admin_cannot_remove_owner(self, api_client, user, other_user):
        ws = _create_ws(other_user)
        _add_member(ws, user, Role.ADMIN)
        api_client.force_authenticate(user=user)
        resp = api_client.delete(f"{BASE}/{ws.id}/members/{other_user.id}")
        assert resp.status_code == 403
        assert WorkspaceMember.objects.filter(
            workspace=ws, user=other_user
        ).exists()

    def test_owner_can_remove_co_owner(self, authed_client, user, other_user):
        ws = _create_ws(user)
        _add_member(ws, other_user, Role.OWNER)
        resp = authed_client.delete(f"{BASE}/{ws.id}/members/{other_user.id}")
        assert resp.status_code == 204
        assert not WorkspaceMember.objects.filter(
            workspace=ws, user=other_user
        ).exists()

    def test_remove_unknown_member_404(self, authed_client, user, other_user):
        ws = _create_ws(user)
        resp = authed_client.delete(f"{BASE}/{ws.id}/members/{other_user.id}")
        assert resp.status_code == 404
