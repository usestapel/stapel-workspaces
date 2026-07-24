"""Billing entitlement seam tests (org-program spec §D2).

Billing is NOT installed in this suite — the degrade-ALLOW path is the
default. Enforcement is exercised by registering a fake
``billing.check_entitlement`` comm Function provider (the real one lives in
stapel-billing; only its payload contract is shared).
"""

import pytest
from django.utils import timezone

from stapel_core.comm.registry import function_registry

from stapel_workspaces import entitlements
from stapel_workspaces.errors import (
    ERR_402_ENTITLEMENT_REQUIRED,
    ERR_402_MEMBER_LIMIT_REACHED,
)
from stapel_workspaces.models import Role, WorkspaceInvitation, WorkspaceMember

BASE = "/workspaces/api/workspaces"


def _create_ws(user, name="Acme", **kwargs):
    from stapel_workspaces.services import create_workspace

    return create_workspace(user=user, name=name, **kwargs)


@pytest.fixture
def fake_billing():
    """Register a scripted billing.check_entitlement provider.

    Returns a dict the test mutates: set ``response`` (dict or callable) to
    steer verdicts; ``calls`` collects received payloads.
    """
    state = {"response": {"allowed": True}, "calls": []}

    def provider(payload):
        state["calls"].append(payload)
        response = state["response"]
        return response(payload) if callable(response) else response

    function_registry.register(entitlements.CHECK_ENTITLEMENT, provider)
    yield state
    function_registry._providers.pop(entitlements.CHECK_ENTITLEMENT, None)
    function_registry._schemas.pop(entitlements.CHECK_ENTITLEMENT, None)


@pytest.mark.django_db
class TestDegradeAllow:
    """Without billing installed every entitlement check allows (OSS default)."""

    def test_check_entitlement_degrades_to_allow(self, user):
        result = entitlements.check_entitlement(user.pk, entitlements.ENT_ORG)
        assert result.allowed is True
        assert result.reason == "billing_not_installed"

    def test_work_workspace_creation_unrestricted(self, authed_client):
        resp = authed_client.post(
            BASE, {"name": "Org", "type": "work"}, format="json"
        )
        assert resp.status_code == 201, resp.content

    def test_invite_and_accept_unrestricted(self, authed_client, user, other_user):
        ws = _create_ws(user)
        resp = authed_client.post(
            f"{BASE}/{ws.id}/members/invite",
            {"emails": [other_user.email], "role": "member"},
            format="json",
        )
        assert resp.status_code == 201, resp.content


@pytest.mark.django_db
class TestOrgEntitlement:
    def test_denied_org_creation_402(self, authed_client, fake_billing):
        fake_billing["response"] = {"allowed": False, "reason": "plan"}
        resp = authed_client.post(
            BASE, {"name": "Org", "type": "work"}, format="json"
        )
        assert resp.status_code == 402
        assert resp.json()["localizable_error"] == ERR_402_ENTITLEMENT_REQUIRED
        assert fake_billing["calls"][-1]["key"] == entitlements.ENT_ORG

    def test_personal_workspace_never_gated(self, authed_client, fake_billing):
        fake_billing["response"] = {"allowed": False}
        resp = authed_client.post(
            BASE, {"name": "Me", "type": "personal"}, format="json"
        )
        assert resp.status_code == 201, resp.content
        assert fake_billing["calls"] == []

    def test_allowed_org_creation_passes_owner_anchor(
        self, authed_client, user, fake_billing
    ):
        resp = authed_client.post(
            BASE, {"name": "Org", "type": "work"}, format="json"
        )
        assert resp.status_code == 201, resp.content
        call = fake_billing["calls"][-1]
        assert call == {
            "user_id": str(user.pk), "key": entitlements.ENT_ORG, "quantity": 1,
        }


@pytest.mark.django_db
class TestMemberLimit:
    def test_invite_over_limit_402_with_limit_param(
        self, authed_client, user, fake_billing
    ):
        ws = _create_ws(user)
        fake_billing["response"] = {"allowed": False, "limit": 1, "reason": "limit"}
        resp = authed_client.post(
            f"{BASE}/{ws.id}/members/invite",
            {"emails": ["a@example.com"]},
            format="json",
        )
        assert resp.status_code == 402
        body = resp.json()
        assert body["localizable_error"] == ERR_402_MEMBER_LIMIT_REACHED
        assert body["params"] == {"limit": 1}
        assert not WorkspaceInvitation.objects.exists()
        # quantity = 1 accepted (owner) + 0 pending + 1 new invite
        call = fake_billing["calls"][-1]
        assert call["key"] == entitlements.ENT_MEMBERS_MAX
        assert call["quantity"] == 2

    def test_invite_quantity_counts_accepted_pending_and_batch(
        self, authed_client, user, other_user, fake_billing
    ):
        ws = _create_ws(user)
        WorkspaceMember.objects.create(
            workspace=ws, user=other_user, role=Role.MEMBER,
            accepted_at=timezone.now(),
        )
        authed_client.post(
            f"{BASE}/{ws.id}/members/invite",
            {"emails": ["a@example.com"]},
            format="json",
        )
        resp = authed_client.post(
            f"{BASE}/{ws.id}/members/invite",
            {"emails": ["b@example.com", "c@example.com"]},
            format="json",
        )
        assert resp.status_code == 201, resp.content
        # 2 accepted + 1 pending + 2 new
        assert fake_billing["calls"][-1]["quantity"] == 5

    def test_accept_rechecks_limit_402(
        self, authed_client, api_client, user, other_user, fake_billing
    ):
        ws = _create_ws(user)
        resp = authed_client.post(
            f"{BASE}/{ws.id}/members/invite",
            {"emails": [other_user.email]},
            format="json",
        )
        assert resp.status_code == 201, resp.content
        token = WorkspaceInvitation.objects.get().token
        # The plan shrank between invite and accept.
        fake_billing["response"] = {"allowed": False, "limit": 1}
        api_client.force_authenticate(user=other_user)
        resp = api_client.post(
            f"{BASE}/invitations/accept", {"token": token}, format="json"
        )
        assert resp.status_code == 402
        assert resp.json()["localizable_error"] == ERR_402_MEMBER_LIMIT_REACHED
        assert resp.json()["params"] == {"limit": 1}
        # nothing consumed, nothing joined
        assert WorkspaceInvitation.objects.get().accepted_at is None
        assert not WorkspaceMember.objects.filter(
            workspace=ws, user=other_user
        ).exists()

    def test_accept_within_limit_passes(
        self, authed_client, api_client, user, other_user, fake_billing
    ):
        ws = _create_ws(user)
        authed_client.post(
            f"{BASE}/{ws.id}/members/invite",
            {"emails": [other_user.email]},
            format="json",
        )
        token = WorkspaceInvitation.objects.get().token
        api_client.force_authenticate(user=other_user)
        resp = api_client.post(
            f"{BASE}/invitations/accept", {"token": token}, format="json"
        )
        assert resp.status_code == 200, resp.content
        # accept quantity: invite still pending at check time = already
        # counted (1 accepted + 1 pending, additional=0)
        assert fake_billing["calls"][-1]["quantity"] == 2

    def test_reaccept_for_existing_member_not_blocked(
        self, user, other_user, fake_billing
    ):
        from stapel_workspaces.services import accept_invitation, create_invitation

        ws = _create_ws(user)
        WorkspaceMember.objects.create(
            workspace=ws, user=other_user, role=Role.MEMBER,
            accepted_at=timezone.now(),
        )
        inv = create_invitation(
            workspace=ws, email=other_user.email, role="member", invited_by=user
        )
        fake_billing["response"] = {"allowed": False, "limit": 1}
        member = accept_invitation(invitation=inv, user=other_user)
        assert member.role == Role.MEMBER  # no seat added, no block


@pytest.mark.django_db
class TestSeatCounting:
    def test_member_seats_quantity_ignores_dead_invitations(self, user):
        from datetime import timedelta

        from stapel_workspaces.services import create_invitation

        ws = _create_ws(user)
        live = create_invitation(
            workspace=ws, email="live@example.com", role="member", invited_by=user
        )
        expired = create_invitation(
            workspace=ws, email="old@example.com", role="member", invited_by=user
        )
        expired.expires_at = timezone.now() - timedelta(days=1)
        expired.save(update_fields=["expires_at"])
        revoked = create_invitation(
            workspace=ws, email="rev@example.com", role="member", invited_by=user
        )
        revoked.revoked_at = timezone.now()
        revoked.save(update_fields=["revoked_at"])
        assert live.accepted_at is None
        # 1 accepted owner + 1 live pending
        assert entitlements.member_seats_quantity(ws) == 2
        assert entitlements.member_seats_quantity(ws, additional=3) == 5
