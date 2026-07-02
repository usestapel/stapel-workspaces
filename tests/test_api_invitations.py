"""API tests for invitation creation and acceptance."""

from datetime import timedelta

import pytest
from django.utils import timezone

from stapel_workspaces.errors import (
    ERR_400_INVITATION_ALREADY_USED,
    ERR_400_INVITATION_EXPIRED,
    ERR_400_INVITATION_REVOKED,
    ERR_404_INVITATION_NOT_FOUND,
    ERR_404_WORKSPACE_NOT_FOUND,
)
from stapel_workspaces.models import Role, WorkspaceMember

BASE = "/workspaces/api/workspaces"
ACCEPT = f"{BASE}/invitations/accept"


def _create_ws(user, name="Acme"):
    from stapel_workspaces.services import create_workspace

    return create_workspace(user=user, name=name)


def _invite(ws, email, inviter, role=Role.MEMBER):
    from stapel_workspaces.services import create_invitation

    return create_invitation(
        workspace=ws, email=email, role=role, invited_by=inviter
    )


@pytest.mark.django_db
class TestInviteCreate:
    def test_requires_auth(self, api_client, user):
        ws = _create_ws(user)
        resp = api_client.post(
            f"{BASE}/{ws.id}/members/invite",
            {"emails": ["a@example.com"], "role": "member"},
            format="json",
        )
        assert resp.status_code in (401, 403)

    def test_invite_normalizes_emails(self, authed_client, user):
        ws = _create_ws(user)
        resp = authed_client.post(
            f"{BASE}/{ws.id}/members/invite",
            {"emails": ["  Mixed.Case@Example.COM "], "role": "viewer"},
            format="json",
        )
        assert resp.status_code == 201, resp.content
        inv = resp.json()["invitations"][0]
        assert inv["email"] == "mixed.case@example.com"
        assert inv["role"] == Role.VIEWER
        assert ws.invitations.get().token  # token stored, not exposed logic

    def test_invite_role_owner_rejected(self, authed_client, user):
        ws = _create_ws(user)
        resp = authed_client.post(
            f"{BASE}/{ws.id}/members/invite",
            {"emails": ["a@example.com"], "role": "owner"},
            format="json",
        )
        assert resp.status_code == 400
        assert ws.invitations.count() == 0

    def test_invite_empty_emails_rejected(self, authed_client, user):
        ws = _create_ws(user)
        resp = authed_client.post(
            f"{BASE}/{ws.id}/members/invite",
            {"emails": [], "role": "member"},
            format="json",
        )
        assert resp.status_code == 400

    def test_invite_into_deleted_workspace_404(self, authed_client, user):
        ws = _create_ws(user)
        ws.deleted_at = timezone.now()
        ws.save(update_fields=["deleted_at"])
        resp = authed_client.post(
            f"{BASE}/{ws.id}/members/invite",
            {"emails": ["a@example.com"], "role": "member"},
            format="json",
        )
        assert resp.status_code == 404
        assert resp.json()["localizable_error"] == ERR_404_WORKSPACE_NOT_FOUND


@pytest.mark.django_db
class TestInvitationAccept:
    def _accept(self, client, token):
        return client.post(ACCEPT, {"token": token}, format="json")

    def test_requires_auth(self, api_client, user):
        ws = _create_ws(user)
        inv = _invite(ws, "someone@example.com", user)
        resp = self._accept(api_client, inv.token)
        assert resp.status_code in (401, 403)

    def test_accept_creates_membership(self, api_client, user, other_user):
        ws = _create_ws(user)
        inv = _invite(ws, other_user.email, user, role=Role.ADMIN)
        api_client.force_authenticate(user=other_user)

        resp = self._accept(api_client, inv.token)
        assert resp.status_code == 200, resp.content
        data = resp.json()
        assert data["user_id"] == str(other_user.id)
        assert data["role"] == Role.ADMIN
        member = WorkspaceMember.objects.get(workspace=ws, user=other_user)
        assert member.role == Role.ADMIN
        assert member.accepted_at is not None
        inv.refresh_from_db()
        assert inv.accepted_at is not None

    def test_accept_case_insensitive_email(self, api_client, user, other_user):
        ws = _create_ws(user)
        inv = _invite(ws, other_user.email.upper(), user)
        api_client.force_authenticate(user=other_user)
        assert self._accept(api_client, inv.token).status_code == 200

    def test_missing_token_field_rejected(self, api_client, other_user):
        api_client.force_authenticate(user=other_user)
        resp = api_client.post(ACCEPT, {}, format="json")
        assert resp.status_code == 400

    def test_unknown_token_404(self, api_client, other_user, db):
        api_client.force_authenticate(user=other_user)
        resp = self._accept(api_client, "no-such-token")
        assert resp.status_code == 404
        assert resp.json()["localizable_error"] == ERR_404_INVITATION_NOT_FOUND

    def test_revoked_invitation_400(self, api_client, user, other_user):
        ws = _create_ws(user)
        inv = _invite(ws, other_user.email, user)
        inv.revoked_at = timezone.now()
        inv.save(update_fields=["revoked_at"])
        api_client.force_authenticate(user=other_user)
        resp = self._accept(api_client, inv.token)
        assert resp.status_code == 400
        assert resp.json()["localizable_error"] == ERR_400_INVITATION_REVOKED
        assert not WorkspaceMember.objects.filter(
            workspace=ws, user=other_user
        ).exists()

    def test_already_used_invitation_400(self, api_client, user, other_user):
        ws = _create_ws(user)
        inv = _invite(ws, other_user.email, user)
        inv.accepted_at = timezone.now()
        inv.save(update_fields=["accepted_at"])
        api_client.force_authenticate(user=other_user)
        resp = self._accept(api_client, inv.token)
        assert resp.status_code == 400
        assert resp.json()["localizable_error"] == ERR_400_INVITATION_ALREADY_USED

    def test_expired_invitation_400(self, api_client, user, other_user):
        ws = _create_ws(user)
        inv = _invite(ws, other_user.email, user)
        inv.expires_at = timezone.now() - timedelta(days=1)
        inv.save(update_fields=["expires_at"])
        api_client.force_authenticate(user=other_user)
        resp = self._accept(api_client, inv.token)
        assert resp.status_code == 400
        assert resp.json()["localizable_error"] == ERR_400_INVITATION_EXPIRED

    def test_foreign_email_cannot_use_token(self, api_client, user, other_user):
        """The token is personal: another account gets 404, no membership."""
        ws = _create_ws(user)
        inv = _invite(ws, "invitee@example.com", user)
        api_client.force_authenticate(user=other_user)
        resp = self._accept(api_client, inv.token)
        assert resp.status_code == 404
        assert resp.json()["localizable_error"] == ERR_404_INVITATION_NOT_FOUND
        assert not WorkspaceMember.objects.filter(
            workspace=ws, user=other_user
        ).exists()
        inv.refresh_from_db()
        assert inv.accepted_at is None

    def test_concurrent_accept_race_returns_400(
        self, api_client, user, other_user, monkeypatch
    ):
        """If the row was consumed between the checks and the locked update,
        the service raises ValueError and the API answers 400."""
        from stapel_workspaces import views as views_mod

        ws = _create_ws(user)
        inv = _invite(ws, other_user.email, user)

        def raced(**kwargs):
            raise ValueError("invitation already used")

        monkeypatch.setattr(views_mod.services, "accept_invitation", raced)
        api_client.force_authenticate(user=other_user)
        resp = self._accept(api_client, inv.token)
        assert resp.status_code == 400
        assert resp.json()["localizable_error"] == ERR_400_INVITATION_ALREADY_USED

    def test_deleted_workspace_404(self, api_client, user, other_user):
        ws = _create_ws(user)
        inv = _invite(ws, other_user.email, user)
        ws.deleted_at = timezone.now()
        ws.save(update_fields=["deleted_at"])
        api_client.force_authenticate(user=other_user)
        resp = self._accept(api_client, inv.token)
        assert resp.status_code == 404


@pytest.mark.django_db
class TestAcceptInvitationService:
    def test_double_accept_raises(self, user, other_user):
        from stapel_workspaces.services import accept_invitation

        ws = _create_ws(user)
        inv = _invite(ws, other_user.email, user)
        accept_invitation(invitation=inv, user=other_user)
        with pytest.raises(ValueError):
            accept_invitation(invitation=inv, user=other_user)
