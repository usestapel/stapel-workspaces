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


def _named_user(first, last, email):
    from stapel_core.django.users.models import User

    return User.objects.create_user(
        username=f"u-{uuid.uuid4().hex[:8]}",
        email=email,
        first_name=first,
        last_name=last,
        password="testpass-1234",
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
class TestMemberListSearchPagination:
    """?search= + limit/offset + stable display-name sort (BACKLOG G12)."""

    def test_search_by_email(self, authed_client, user):
        ws = _create_ws(user)
        alice = _named_user("Alice", "Anderson", "alice@picker.test")
        _add_member(ws, alice, Role.MEMBER)
        _add_member(ws, _named_user("Bob", "Brown", "bob@picker.test"), Role.MEMBER)
        resp = authed_client.get(f"{BASE}/{ws.id}/members?search=ALICE@pick")
        assert resp.status_code == 200, resp.content
        ids = [m["user_id"] for m in resp.json()["members"]]
        assert ids == [str(alice.id)]

    def test_search_by_display_name_case_insensitive(self, authed_client, user):
        ws = _create_ws(user)
        alice = _named_user("Alice", "Anderson", "alice@picker.test")
        _add_member(ws, alice, Role.MEMBER)
        _add_member(ws, _named_user("Bob", "Brown", "bob@picker.test"), Role.MEMBER)
        # last name
        resp = authed_client.get(f"{BASE}/{ws.id}/members?search=anderson")
        assert [m["user_id"] for m in resp.json()["members"]] == [str(alice.id)]
        # full display name ("Alice Anderson") also matches
        resp2 = authed_client.get(f"{BASE}/{ws.id}/members?search=alice ander")
        assert [m["user_id"] for m in resp2.json()["members"]] == [str(alice.id)]

    def test_search_empty_result(self, authed_client, user):
        ws = _create_ws(user)
        _add_member(
            ws, _named_user("Alice", "Anderson", "alice@picker.test"), Role.MEMBER
        )
        resp = authed_client.get(f"{BASE}/{ws.id}/members?search=nobody-zzz")
        assert resp.status_code == 200
        assert resp.json()["members"] == []

    def test_stable_sort_by_display_name(self, authed_client, user):
        ws = _create_ws(user)
        cara = _named_user("Cara", "C", "cara@picker.test")
        anna = _named_user("Anna", "A", "anna@picker.test")
        bella = _named_user("Bella", "B", "bella@picker.test")
        for u in (cara, anna, bella):  # inserted out of order
            _add_member(ws, u, Role.MEMBER)
        # scope out the owner (its email domain differs) to assert pure order
        resp = authed_client.get(f"{BASE}/{ws.id}/members?search=picker.test")
        ids = [m["user_id"] for m in resp.json()["members"]]
        assert ids == [str(anna.id), str(bella.id), str(cara.id)]

    def test_pagination_limit_offset(self, authed_client, user):
        ws = _create_ws(user)
        for i in range(4):
            _add_member(
                ws, _named_user(f"M{i}", "X", f"m{i}@picker.test"), Role.MEMBER
            )
        base = f"{BASE}/{ws.id}/members"
        full = [m["user_id"] for m in authed_client.get(base).json()["members"]]
        assert len(full) == 5  # 4 members + owner
        page1 = authed_client.get(f"{base}?limit=2&offset=0").json()["members"]
        page2 = authed_client.get(f"{base}?limit=2&offset=2").json()["members"]
        assert [m["user_id"] for m in page1] == full[:2]
        assert [m["user_id"] for m in page2] == full[2:4]

    def test_offset_without_limit(self, authed_client, user):
        ws = _create_ws(user)
        for i in range(3):
            _add_member(
                ws, _named_user(f"M{i}", "X", f"m{i}@picker.test"), Role.MEMBER
            )
        base = f"{BASE}/{ws.id}/members"
        full = [m["user_id"] for m in authed_client.get(base).json()["members"]]
        tail = authed_client.get(f"{base}?offset=2").json()["members"]
        assert [m["user_id"] for m in tail] == full[2:]

    def test_no_params_returns_all_backward_compatible(self, authed_client, user):
        ws = _create_ws(user)
        _add_member(
            ws, _named_user("Alice", "Anderson", "alice@picker.test"), Role.MEMBER
        )
        resp = authed_client.get(f"{BASE}/{ws.id}/members")
        assert resp.status_code == 200
        assert len(resp.json()["members"]) == 2  # owner + member

    def test_invalid_pagination_params_ignored(self, authed_client, user):
        ws = _create_ws(user)
        _add_member(
            ws, _named_user("Alice", "Anderson", "alice@picker.test"), Role.MEMBER
        )
        resp = authed_client.get(f"{BASE}/{ws.id}/members?limit=abc&offset=-5")
        assert resp.status_code == 200
        assert len(resp.json()["members"]) == 2

    def test_search_foreign_workspace_403(self, api_client, user, other_user):
        ws = _create_ws(other_user)
        api_client.force_authenticate(user=user)
        resp = api_client.get(f"{BASE}/{ws.id}/members?search=alice")
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
