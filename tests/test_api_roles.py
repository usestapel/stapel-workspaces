"""Roles endpoint + my_capabilities surface + registry-driven role validation."""

import pytest
from django.test import override_settings
from django.utils import timezone

from stapel_workspaces.errors import ERR_400_INVALID_ROLE
from stapel_workspaces.models import Role, WorkspaceMember

BASE = "/workspaces/api/workspaces"

SECRETARY = {
    "rank": 250,
    "capabilities": ["workspace.view", "members.view", "meetings.spotlight"],
}


def _create_ws(user, name="Acme", **kwargs):
    from stapel_workspaces.services import create_workspace

    return create_workspace(user=user, name=name, **kwargs)


@pytest.mark.django_db
class TestRolesEndpoint:
    def test_requires_auth(self, api_client):
        assert api_client.get(f"{BASE}/roles").status_code in (401, 403)

    def test_returns_builtin_four_sorted_by_rank_desc(self, authed_client):
        resp = authed_client.get(f"{BASE}/roles")
        assert resp.status_code == 200, resp.content
        roles = resp.json()["roles"]
        assert [r["role"] for r in roles] == ["owner", "admin", "member", "viewer"]
        assert all(r["builtin"] for r in roles)
        owner = roles[0]
        assert owner == {
            "role": "owner", "rank": 400, "capabilities": ["*"], "builtin": True,
        }

    @override_settings(STAPEL_WORKSPACES={"ROLES": {"secretary": SECRETARY}})
    def test_overlay_role_listed_with_builtin_false(self, authed_client):
        resp = authed_client.get(f"{BASE}/roles")
        roles = {r["role"]: r for r in resp.json()["roles"]}
        assert roles["secretary"]["builtin"] is False
        assert roles["secretary"]["rank"] == 250
        assert roles["secretary"]["capabilities"] == SECRETARY["capabilities"]
        # rank ordering: admin(300) > secretary(250) > member(200)
        names = [r["role"] for r in resp.json()["roles"]]
        assert names.index("admin") < names.index("secretary") < names.index("member")


@pytest.mark.django_db
class TestMyCapabilities:
    def test_detail_carries_my_capabilities(self, authed_client, user):
        ws = _create_ws(user)
        resp = authed_client.get(f"{BASE}/{ws.id}")
        assert resp.status_code == 200, resp.content
        data = resp.json()
        assert data["my_role"] == "owner"
        assert data["my_capabilities"] == ["*"]

    def test_list_carries_my_capabilities(self, api_client, user, other_user):
        ws = _create_ws(other_user)
        WorkspaceMember.objects.create(
            workspace=ws, user=user, role=Role.VIEWER, accepted_at=timezone.now()
        )
        api_client.force_authenticate(user=user)
        resp = api_client.get(BASE)
        assert resp.status_code == 200
        (entry,) = resp.json()["workspaces"]
        assert entry["my_role"] == "viewer"
        assert entry["my_capabilities"] == ["workspace.view", "members.view"]


@pytest.mark.django_db
class TestRegistryDrivenRoleValidation:
    @override_settings(STAPEL_WORKSPACES={"ROLES": {"secretary": SECRETARY}})
    def test_invite_with_custom_role_accepted(self, authed_client, user):
        ws = _create_ws(user)
        resp = authed_client.post(
            f"{BASE}/{ws.id}/members/invite",
            {"emails": ["sec@example.com"], "role": "secretary"},
            format="json",
        )
        assert resp.status_code == 201, resp.content
        assert resp.json()["invitations"][0]["role"] == "secretary"

    def test_invite_with_unknown_role_rejected(self, authed_client, user):
        ws = _create_ws(user)
        resp = authed_client.post(
            f"{BASE}/{ws.id}/members/invite",
            {"emails": ["x@example.com"], "role": "secretary"},
            format="json",
        )
        assert resp.status_code == 400
        assert ERR_400_INVALID_ROLE in str(resp.json())

    def test_invite_with_owner_role_still_rejected(self, authed_client, user):
        ws = _create_ws(user)
        resp = authed_client.post(
            f"{BASE}/{ws.id}/members/invite",
            {"emails": ["x@example.com"], "role": "owner"},
            format="json",
        )
        assert resp.status_code == 400
        assert ERR_400_INVALID_ROLE in str(resp.json())

    @override_settings(STAPEL_WORKSPACES={"ROLES": {"secretary": SECRETARY}})
    def test_role_change_to_custom_role(self, authed_client, user, other_user):
        ws = _create_ws(user)
        WorkspaceMember.objects.create(
            workspace=ws, user=other_user, role=Role.MEMBER,
            accepted_at=timezone.now(),
        )
        resp = authed_client.patch(
            f"{BASE}/{ws.id}/members/{other_user.id}",
            {"role": "secretary"},
            format="json",
        )
        assert resp.status_code == 200, resp.content
        assert resp.json()["role"] == "secretary"
        member = WorkspaceMember.objects.get(workspace=ws, user=other_user)
        assert member.role == "secretary"

    def test_role_change_to_unknown_role_rejected(self, authed_client, user, other_user):
        ws = _create_ws(user)
        WorkspaceMember.objects.create(
            workspace=ws, user=other_user, role=Role.MEMBER,
            accepted_at=timezone.now(),
        )
        resp = authed_client.patch(
            f"{BASE}/{ws.id}/members/{other_user.id}",
            {"role": "secretary"},
            format="json",
        )
        assert resp.status_code == 400
        assert ERR_400_INVALID_ROLE in str(resp.json())
