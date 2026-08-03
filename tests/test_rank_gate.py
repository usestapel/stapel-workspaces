"""Rank-gard: a capability grants "may hand out roles at all", never "up to
which rank" (mandate-model vardict 2026-08-03).

The gap: ``members.invite`` / ``members.role.change`` / ``members.provision``
sit on ``admin``(300)/``owner`` today, so "admin can only mint another admin"
holds by COINCIDENCE — the capability happens to live only on roles whose
rank ceiling is already low. The coincidence breaks the moment a product
declares a role BELOW admin that also carries the capability (a "manager",
exactly what the owner's mandate decision names) — that role could otherwise
hand out ranks above its own. These tests pin the ceiling directly, with a
below-admin custom role, so the fix is proven independent of "admin/owner
happen to be the only holders".
"""
import uuid as _uuid

import pytest
from django.test import override_settings
from django.utils import timezone

from stapel_core.comm.registry import function_registry
from stapel_core.verification import grant_verification

from stapel_workspaces import services
from stapel_workspaces.capabilities import role_exceeds_rank
from stapel_workspaces.errors import (
    ERR_403_FORBIDDEN_WORKSPACE,
    ERR_403_ROLE_EXCEEDS_INVITER_RANK,
)
from stapel_workspaces.models import Role, WorkspaceMember
from stapel_workspaces.services import create_workspace

BASE = "/workspaces/api/workspaces/v1"

#: Below admin (300) but carries every rank-gated capability — the "manager"
#: shape the owner's mandate decision names (invite-time roles below admin
#: that may still invite). Exists purely to prove the ceiling is not
#: admin/owner-shaped.
MANAGER = {
    "rank": 250,
    "capabilities": [
        "workspace.view", "members.view",
        "members.invite", "members.role.change", "members.provision",
    ],
}

_manager_role = override_settings(STAPEL_WORKSPACES={"ROLES": {"manager": MANAGER}})


def _add_member(ws, u, role, accepted=True):
    return WorkspaceMember.objects.create(
        workspace=ws, user=u, role=role,
        accepted_at=timezone.now() if accepted else None,
    )


def _third_user(username):
    from stapel_core.django.users.models import User

    return User.objects.create_user(
        username=username, email=f"{username}@example.com", password="testpass-1234"
    )


class TestRoleExceedsRankPure:
    def test_equal_rank_does_not_exceed(self):
        assert role_exceeds_rank(Role.ADMIN, Role.ADMIN) is False

    def test_lower_rank_does_not_exceed(self):
        assert role_exceeds_rank(Role.MEMBER, Role.ADMIN) is False

    def test_higher_rank_exceeds(self):
        assert role_exceeds_rank(Role.ADMIN, Role.MEMBER) is True

    def test_owner_always_exceeds_a_non_owner_actor(self):
        assert role_exceeds_rank(Role.OWNER, Role.ADMIN) is True

    def test_unknown_role_fails_closed(self):
        assert role_exceeds_rank("ghost", Role.OWNER) is True
        assert role_exceeds_rank(Role.MEMBER, "ghost") is True

    @_manager_role
    def test_manager_below_admin_cannot_reach_admin(self):
        assert role_exceeds_rank(Role.ADMIN, "manager") is True
        assert role_exceeds_rank(Role.MEMBER, "manager") is False
        assert role_exceeds_rank("manager", "manager") is False


@pytest.mark.django_db
class TestInviteRankGate:
    @_manager_role
    def test_manager_cannot_invite_an_admin(self, api_client, user, other_user):
        ws = create_workspace(user=other_user, name="Acme")
        _add_member(ws, user, "manager")
        api_client.force_authenticate(user=user)
        resp = api_client.post(
            f"{BASE}/{ws.id}/members/invite",
            {"emails": ["x@example.com"], "role": "admin"},
            format="json",
        )
        assert resp.status_code == 403, resp.content
        assert resp.json()["localizable_error"] == ERR_403_ROLE_EXCEEDS_INVITER_RANK
        assert not ws.invitations.exists()

    @_manager_role
    def test_manager_can_invite_a_member(self, api_client, user, other_user):
        ws = create_workspace(user=other_user, name="Acme")
        _add_member(ws, user, "manager")
        api_client.force_authenticate(user=user)
        resp = api_client.post(
            f"{BASE}/{ws.id}/members/invite",
            {"emails": ["x@example.com"], "role": "member"},
            format="json",
        )
        assert resp.status_code == 201, resp.content

    @_manager_role
    def test_manager_can_invite_another_manager(self, api_client, user, other_user):
        """Equal rank is allowed — the ceiling is "not above", not "below"."""
        ws = create_workspace(user=other_user, name="Acme")
        _add_member(ws, user, "manager")
        api_client.force_authenticate(user=user)
        resp = api_client.post(
            f"{BASE}/{ws.id}/members/invite",
            {"emails": ["x@example.com"], "role": "manager"},
            format="json",
        )
        assert resp.status_code == 201, resp.content

    def test_admin_inviting_admin_is_unaffected(self, authed_client, user, other_user):
        """The existing, coincidental "admin caps at admin" behavior must
        keep working once the ceiling is an explicit rule."""
        ws = create_workspace(user=user, name="Acme")
        resp = authed_client.post(
            f"{BASE}/{ws.id}/members/invite",
            {"emails": ["x@example.com"], "role": "admin"},
            format="json",
        )
        assert resp.status_code == 201, resp.content


@pytest.mark.django_db
class TestRoleChangeRankGate:
    @_manager_role
    def test_manager_cannot_promote_a_member_to_admin(
        self, api_client, user, other_user
    ):
        ws = create_workspace(user=other_user, name="Acme")
        _add_member(ws, user, "manager")
        # other_user is the workspace's OWNER (created it) — a third account
        # is the actual target of the role change.
        third = _third_user("third")
        target = _add_member(ws, third, Role.MEMBER)
        api_client.force_authenticate(user=user)
        resp = api_client.patch(
            f"{BASE}/{ws.id}/members/{third.id}", {"role": "admin"}, format="json"
        )
        assert resp.status_code == 403, resp.content
        assert resp.json()["localizable_error"] == ERR_403_ROLE_EXCEEDS_INVITER_RANK
        target.refresh_from_db()
        assert target.role == Role.MEMBER

    @_manager_role
    def test_manager_can_demote_an_admin_to_member(self, api_client, user, other_user):
        """Demotion (target rank below actor's) must still work — the
        ceiling only blocks handing out MORE than the actor holds."""
        ws = create_workspace(user=other_user, name="Acme")
        _add_member(ws, user, "manager")
        third = _third_user("third2")
        target = _add_member(ws, third, "manager")
        api_client.force_authenticate(user=user)
        resp = api_client.patch(
            f"{BASE}/{ws.id}/members/{third.id}", {"role": "member"}, format="json"
        )
        assert resp.status_code == 200, resp.content
        target.refresh_from_db()
        assert target.role == Role.MEMBER

    def test_admin_cannot_grant_owner_error_key_unchanged(
        self, api_client, user, other_user
    ):
        """Existing owner-escalation guard keeps ITS OWN error key — the
        rank-gard must not shadow it with role_exceeds_inviter_rank."""
        ws = create_workspace(user=other_user, name="Acme")
        me = _add_member(ws, user, Role.ADMIN)
        api_client.force_authenticate(user=user)
        resp = api_client.patch(
            f"{BASE}/{ws.id}/members/{user.id}", {"role": "owner"}, format="json"
        )
        assert resp.status_code == 403
        assert resp.json()["localizable_error"] == ERR_403_FORBIDDEN_WORKSPACE
        me.refresh_from_db()
        assert me.role == Role.ADMIN


@pytest.fixture
def fake_auth_provision(db):
    """Scripted ``auth.provision_user`` provider (mirrors test_api_provision.py)."""
    from stapel_core.django.users.models import User

    def provider(payload):
        created = User.objects.create_user(
            username=payload["username"],
            email=f"{_uuid.uuid4().hex[:8]}@example.com",
            password="srv-generated-1",
        )
        return {"user_id": str(created.pk), "generated_password": "srv-generated-1"}

    function_registry.register(services.PROVISION_USER, provider)
    yield
    function_registry._providers.pop(services.PROVISION_USER, None)
    function_registry._schemas.pop(services.PROVISION_USER, None)


@pytest.mark.django_db
class TestProvisionRankGate:
    @_manager_role
    def test_manager_cannot_provision_an_admin(
        self, api_client, user, other_user, fake_auth_provision
    ):
        ws = create_workspace(user=other_user, name="Acme")
        _add_member(ws, user, "manager")
        grant_verification(user_id=str(user.pk), scope="sensitive", max_age=300)
        api_client.force_authenticate(user=user)
        resp = api_client.post(
            f"{BASE}/{ws.id}/members/provision",
            {"username_local": "ghost", "role": "admin"},
            format="json",
        )
        assert resp.status_code == 403, resp.content
        assert resp.json()["localizable_error"] == ERR_403_ROLE_EXCEEDS_INVITER_RANK
        assert not WorkspaceMember.objects.filter(
            workspace=ws, invited_by=user, provisioned=True
        ).exists()

    @_manager_role
    def test_manager_can_provision_a_member(
        self, api_client, user, other_user, fake_auth_provision
    ):
        ws = create_workspace(user=other_user, name="Acme")
        _add_member(ws, user, "manager")
        grant_verification(user_id=str(user.pk), scope="sensitive", max_age=300)
        api_client.force_authenticate(user=user)
        resp = api_client.post(
            f"{BASE}/{ws.id}/members/provision",
            {"username_local": "ghost", "role": "member"},
            format="json",
        )
        assert resp.status_code == 201, resp.content
