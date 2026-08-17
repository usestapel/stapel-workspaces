"""API tests for the Wave-2 invite flow (org-program spec §B1-B3):

* ``GET  invitations/<token>``          — AllowAny preview (masked email,
  derived status, email_registered), throttled enumeration backstop;
* ``POST invitations/<token>/decline``  — authenticated + email-match,
  decline ≠ revoke, terminal;
* ``POST invitations/<token>/claim``    — AllowAny login-grant mint for
  unregistered emails via the ``auth.issue_login_grant`` comm Function
  (faked here — the real provider lives in stapel-auth 0.11); degrades to
  an honest 503 when auth is not wired, never to allow.
"""

import logging
from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from stapel_core.comm.registry import function_registry

from stapel_workspaces import services
from stapel_workspaces.errors import (
    ERR_400_INVITATION_ALREADY_USED,
    ERR_400_INVITATION_DECLINED,
    ERR_400_INVITATION_EXPIRED,
    ERR_400_INVITATION_REVOKED,
    ERR_404_INVITATION_NOT_FOUND,
    ERR_409_EMAIL_ALREADY_REGISTERED,
    ERR_503_AUTH_UNAVAILABLE,
)
from stapel_workspaces.models import Role, WorkspaceMember

BASE = "/workspaces/api/workspaces/v1"


def _preview_url(token):
    return f"{BASE}/invitations/{token}"


def _decline_url(token):
    return f"{BASE}/invitations/{token}/decline"


def _claim_url(token):
    return f"{BASE}/invitations/{token}/claim"


def _create_ws(user, name="Acme"):
    return services.create_workspace(user=user, name=name)


def _invite(ws, email, inviter, role=Role.MEMBER):
    return services.create_invitation(
        workspace=ws, email=email, role=role, invited_by=inviter
    )


@pytest.fixture(autouse=True)
def no_invite_throttle(settings):
    """Disable the invitation throttle for this module.

    The AllowAny endpoints share one anonymous per-IP bucket in the
    process-wide locmem cache; without this, unrelated tests in a single
    run would eat each other's rate. The throttle test re-enables it with
    its own rate.
    """
    settings.STAPEL_WORKSPACES = {"INVITATION_THROTTLE": None}


@pytest.fixture
def fake_auth_grant():
    """Register a scripted ``auth.issue_login_grant`` provider.

    Returns a dict the test mutates: set ``response`` (dict or callable) to
    steer results; ``calls`` collects received payloads. Mirrors the
    ``fake_billing`` fixture pattern (test_entitlements.py).
    """
    state = {"response": {"grant_token": "grant-tok-1"}, "calls": []}

    def provider(payload):
        state["calls"].append(payload)
        response = state["response"]
        return response(payload) if callable(response) else response

    function_registry.register(services.ISSUE_LOGIN_GRANT, provider)
    yield state
    function_registry._providers.pop(services.ISSUE_LOGIN_GRANT, None)
    function_registry._schemas.pop(services.ISSUE_LOGIN_GRANT, None)


@pytest.mark.django_db
class TestInvitationPreview:
    def test_anonymous_pending_preview(self, api_client, user):
        ws = _create_ws(user, name="Preview Org")
        inv = _invite(ws, "invitee@example.com", user, role=Role.ADMIN)
        resp = api_client.get(_preview_url(inv.token))
        assert resp.status_code == 200, resp.content
        data = resp.json()
        assert data["workspace_name"] == "Preview Org"
        assert data["role"] == Role.ADMIN
        assert data["status"] == "pending"
        assert data["email_registered"] is False
        assert data["expires_at"] == inv.expires_at.isoformat()

    def test_email_is_masked(self, api_client, user):
        ws = _create_ws(user)
        inv = _invite(ws, "invitee@example.com", user)
        data = api_client.get(_preview_url(inv.token)).json()
        assert data["email_masked"] == "i***@e***.com"
        assert "invitee" not in data["email_masked"]

    def test_email_registered_true_case_insensitive(
        self, api_client, user, other_user
    ):
        ws = _create_ws(user)
        inv = _invite(ws, other_user.email.upper(), user)
        data = api_client.get(_preview_url(inv.token)).json()
        assert data["email_registered"] is True

    def test_status_expired(self, api_client, user):
        ws = _create_ws(user)
        inv = _invite(ws, "a@example.com", user)
        inv.expires_at = timezone.now() - timedelta(days=1)
        inv.save(update_fields=["expires_at"])
        assert api_client.get(_preview_url(inv.token)).json()["status"] == "expired"

    def test_status_revoked(self, api_client, user):
        ws = _create_ws(user)
        inv = _invite(ws, "a@example.com", user)
        inv.revoked_at = timezone.now()
        inv.save(update_fields=["revoked_at"])
        assert api_client.get(_preview_url(inv.token)).json()["status"] == "revoked"

    def test_status_accepted(self, api_client, user, other_user):
        ws = _create_ws(user)
        inv = _invite(ws, other_user.email, user)
        services.accept_invitation(invitation=inv, user=other_user)
        assert api_client.get(_preview_url(inv.token)).json()["status"] == "accepted"

    def test_status_declined(self, api_client, user, other_user):
        ws = _create_ws(user)
        inv = _invite(ws, other_user.email, user)
        services.decline_invitation(invitation=inv, user=other_user)
        assert api_client.get(_preview_url(inv.token)).json()["status"] == "declined"

    def test_revoked_beats_expired(self, api_client, user):
        """Stored terminal states win over the TTL (status precedence)."""
        ws = _create_ws(user)
        inv = _invite(ws, "a@example.com", user)
        inv.revoked_at = timezone.now()
        inv.expires_at = timezone.now() - timedelta(days=1)
        inv.save(update_fields=["revoked_at", "expires_at"])
        assert api_client.get(_preview_url(inv.token)).json()["status"] == "revoked"

    def test_unknown_token_404(self, api_client, db):
        resp = api_client.get(_preview_url("no-such-token"))
        assert resp.status_code == 404
        assert resp.json()["localizable_error"] == ERR_404_INVITATION_NOT_FOUND

    def test_deleted_workspace_404(self, api_client, user):
        ws = _create_ws(user)
        inv = _invite(ws, "a@example.com", user)
        ws.deleted_at = timezone.now()
        ws.save(update_fields=["deleted_at"])
        assert api_client.get(_preview_url(inv.token)).status_code == 404

    def test_throttled(self, api_client, user, settings):
        """The AllowAny surface has an enumeration backstop (spec §B2)."""
        from django.core.cache import cache

        ws = _create_ws(user)
        inv = _invite(ws, "a@example.com", user)
        settings.STAPEL_WORKSPACES = {"INVITATION_THROTTLE": "2/min"}
        cache.clear()  # drop any shared anonymous bucket from earlier tests
        assert api_client.get(_preview_url(inv.token)).status_code == 200
        assert api_client.get(_preview_url(inv.token)).status_code == 200
        assert api_client.get(_preview_url(inv.token)).status_code == 429


@pytest.mark.django_db
class TestInvitationDecline:
    def test_anonymous_may_decline_on_the_token_alone(self, api_client, user):
        """Saying no must not require creating the account being declined.

        The invitee with no account is the majority case on this path — it
        is why ``claim`` exists. While decline required a session, refusing
        meant first registering, and the refusal left a live empty account
        behind it. The token is the proof, and it is the same proof
        ``claim`` already accepts from an anonymous caller for strictly
        more: a login grant for the invited mailbox.
        """
        ws = _create_ws(user)
        inv = _invite(ws, "someone@example.com", user)
        resp = api_client.post(_decline_url(inv.token))
        assert resp.status_code == 204, resp.content
        inv.refresh_from_db()
        assert inv.declined_at is not None
        assert inv.revoked_at is None  # decline ≠ revoke
        # No account was created in order to refuse one.
        assert not get_user_model().objects.filter(
            email__iexact="someone@example.com"
        ).exists()

    def test_an_unknown_token_is_refused_and_a_revoked_one_is_not_declinable(
        self, api_client, user
    ):
        """The token is the whole gate now, so it has to BE a gate.

        Nothing else on this path asserts that: the surviving tests all
        start from a real token. A caller with no session and a made-up
        token must get the same 404 preview gives — the 32-byte token is
        the only secret left — and a token the workspace has already
        withdrawn must stay unusable from an anonymous caller too, not
        only from the signed-in invitee.
        """
        resp = api_client.post(_decline_url("no-such-token"))
        assert resp.status_code == 404
        assert resp.json()["localizable_error"] == ERR_404_INVITATION_NOT_FOUND

        ws = _create_ws(user)
        inv = _invite(ws, "invitee@example.com", user)
        inv.revoked_at = timezone.now()
        inv.save(update_fields=["revoked_at"])
        resp = api_client.post(_decline_url(inv.token))
        assert resp.status_code == 400
        assert resp.json()["localizable_error"] == ERR_400_INVITATION_REVOKED
        inv.refresh_from_db()
        assert inv.declined_at is None

    def test_a_signed_in_stranger_still_cannot_decline(self, api_client, user, other_user):
        """Relaxing the gate for the account-less invitee must not turn into
        a licence to resolve OTHER people's invitations while signed in."""
        ws = _create_ws(user)
        inv = _invite(ws, "invitee@example.com", user)
        api_client.force_authenticate(user=other_user)
        resp = api_client.post(_decline_url(inv.token))
        assert resp.status_code == 404
        inv.refresh_from_db()
        assert inv.declined_at is None

    def test_decline_sets_declined_at(self, api_client, user, other_user):
        ws = _create_ws(user)
        inv = _invite(ws, other_user.email, user)
        api_client.force_authenticate(user=other_user)
        resp = api_client.post(_decline_url(inv.token))
        assert resp.status_code == 204, resp.content
        inv.refresh_from_db()
        assert inv.declined_at is not None
        assert inv.revoked_at is None  # decline ≠ revoke
        assert not WorkspaceMember.objects.filter(
            workspace=ws, user=other_user
        ).exists()

    def test_accept_after_decline_400(self, api_client, user, other_user):
        ws = _create_ws(user)
        inv = _invite(ws, other_user.email, user)
        api_client.force_authenticate(user=other_user)
        assert api_client.post(_decline_url(inv.token)).status_code == 204
        resp = api_client.post(
            f"{BASE}/invitations/accept", {"token": inv.token}, format="json"
        )
        assert resp.status_code == 400
        assert resp.json()["localizable_error"] == ERR_400_INVITATION_DECLINED
        assert not WorkspaceMember.objects.filter(
            workspace=ws, user=other_user
        ).exists()

    def test_double_decline_400(self, api_client, user, other_user):
        ws = _create_ws(user)
        inv = _invite(ws, other_user.email, user)
        api_client.force_authenticate(user=other_user)
        assert api_client.post(_decline_url(inv.token)).status_code == 204
        resp = api_client.post(_decline_url(inv.token))
        assert resp.status_code == 400
        assert resp.json()["localizable_error"] == ERR_400_INVITATION_DECLINED

    def test_foreign_email_404(self, api_client, user, other_user):
        """The token is personal, in both directions: a different account
        must not be able to kill someone else's invitation."""
        ws = _create_ws(user)
        inv = _invite(ws, "invitee@example.com", user)
        api_client.force_authenticate(user=other_user)
        resp = api_client.post(_decline_url(inv.token))
        assert resp.status_code == 404
        inv.refresh_from_db()
        assert inv.declined_at is None

    def test_expired_400(self, api_client, user, other_user):
        ws = _create_ws(user)
        inv = _invite(ws, other_user.email, user)
        inv.expires_at = timezone.now() - timedelta(days=1)
        inv.save(update_fields=["expires_at"])
        api_client.force_authenticate(user=other_user)
        resp = api_client.post(_decline_url(inv.token))
        assert resp.status_code == 400
        assert resp.json()["localizable_error"] == ERR_400_INVITATION_EXPIRED

    def test_revoked_400(self, api_client, user, other_user):
        ws = _create_ws(user)
        inv = _invite(ws, other_user.email, user)
        inv.revoked_at = timezone.now()
        inv.save(update_fields=["revoked_at"])
        api_client.force_authenticate(user=other_user)
        resp = api_client.post(_decline_url(inv.token))
        assert resp.status_code == 400
        assert resp.json()["localizable_error"] == ERR_400_INVITATION_REVOKED

    def test_accepted_400(self, api_client, user, other_user):
        ws = _create_ws(user)
        inv = _invite(ws, other_user.email, user)
        services.accept_invitation(invitation=inv, user=other_user)
        api_client.force_authenticate(user=other_user)
        resp = api_client.post(_decline_url(inv.token))
        assert resp.status_code == 400
        assert resp.json()["localizable_error"] == ERR_400_INVITATION_ALREADY_USED


@pytest.mark.django_db
class TestInvitationClaim:
    def test_claim_mints_grant_and_leaves_invitation_pending(
        self, api_client, user, fake_auth_grant
    ):
        ws = _create_ws(user)
        inv = _invite(ws, "newcomer@example.com", user)
        resp = api_client.post(
            _claim_url(inv.token), HTTP_ACCEPT_LANGUAGE="ru-RU,ru;q=0.9"
        )
        assert resp.status_code == 200, resp.content
        assert resp.json() == {"grant_token": "grant-tok-1"}
        # The grant payload carries the invite's email as verified and asks
        # auth to create the account on exchange, with the UI language hint.
        assert fake_auth_grant["calls"] == [
            {
                "email": "newcomer@example.com",
                "verified_email": True,
                "create_if_missing": True,
                "language": "ru",
            }
        ]
        # Claim does NOT consume the invitation — accept is a separate step.
        inv.refresh_from_db()
        assert inv.accepted_at is None
        assert inv.declined_at is None
        assert inv.status == "pending"

    def test_claim_without_language_header_omits_hint(
        self, api_client, user, fake_auth_grant
    ):
        ws = _create_ws(user)
        inv = _invite(ws, "newcomer@example.com", user)
        assert api_client.post(_claim_url(inv.token)).status_code == 200
        assert "language" not in fake_auth_grant["calls"][0]

    def test_registered_email_409(self, api_client, user, other_user, fake_auth_grant):
        ws = _create_ws(user)
        inv = _invite(ws, other_user.email, user)
        resp = api_client.post(_claim_url(inv.token))
        assert resp.status_code == 409
        assert resp.json()["localizable_error"] == ERR_409_EMAIL_ALREADY_REGISTERED
        assert fake_auth_grant["calls"] == []  # no grant minted

    def test_auth_not_wired_503(self, api_client, user):
        """No auth.issue_login_grant provider → honest 503, never allow
        (an invite flow without auth is meaningless — spec §B2)."""
        ws = _create_ws(user)
        inv = _invite(ws, "newcomer@example.com", user)
        resp = api_client.post(_claim_url(inv.token))
        assert resp.status_code == 503
        assert resp.json()["localizable_error"] == ERR_503_AUTH_UNAVAILABLE

    def test_expired_400(self, api_client, user, fake_auth_grant):
        ws = _create_ws(user)
        inv = _invite(ws, "newcomer@example.com", user)
        inv.expires_at = timezone.now() - timedelta(days=1)
        inv.save(update_fields=["expires_at"])
        resp = api_client.post(_claim_url(inv.token))
        assert resp.status_code == 400
        assert resp.json()["localizable_error"] == ERR_400_INVITATION_EXPIRED
        assert fake_auth_grant["calls"] == []

    def test_revoked_400(self, api_client, user, fake_auth_grant):
        ws = _create_ws(user)
        inv = _invite(ws, "newcomer@example.com", user)
        inv.revoked_at = timezone.now()
        inv.save(update_fields=["revoked_at"])
        resp = api_client.post(_claim_url(inv.token))
        assert resp.status_code == 400
        assert resp.json()["localizable_error"] == ERR_400_INVITATION_REVOKED

    def test_declined_400(self, api_client, user, fake_auth_grant):
        ws = _create_ws(user)
        inv = _invite(ws, "newcomer@example.com", user)
        inv.declined_at = timezone.now()
        inv.save(update_fields=["declined_at"])
        resp = api_client.post(_claim_url(inv.token))
        assert resp.status_code == 400
        assert resp.json()["localizable_error"] == ERR_400_INVITATION_DECLINED

    def test_accepted_400(self, api_client, user, other_user, fake_auth_grant):
        ws = _create_ws(user)
        inv = _invite(ws, other_user.email, user)
        services.accept_invitation(invitation=inv, user=other_user)
        other_user.delete()  # make the email unregistered again
        resp = api_client.post(_claim_url(inv.token))
        assert resp.status_code == 400
        assert resp.json()["localizable_error"] == ERR_400_INVITATION_ALREADY_USED

    def test_unknown_token_404(self, api_client, db):
        assert api_client.post(_claim_url("no-such-token")).status_code == 404

    def test_deleted_workspace_404(self, api_client, user, fake_auth_grant):
        ws = _create_ws(user)
        inv = _invite(ws, "newcomer@example.com", user)
        ws.deleted_at = timezone.now()
        ws.save(update_fields=["deleted_at"])
        assert api_client.post(_claim_url(inv.token)).status_code == 404


@pytest.mark.django_db
class TestTokenNotLogged:
    def test_flow_never_logs_the_token(
        self, api_client, user, caplog, fake_auth_grant
    ):
        """The invite token is a bearer credential: creation (incl. the
        notification path), preview, claim and decline must not write it to
        any log record."""
        with caplog.at_level(logging.DEBUG):
            ws = _create_ws(user)
            inv = _invite(ws, "newcomer@example.com", user)
            api_client.get(_preview_url(inv.token))
            api_client.post(_claim_url(inv.token))
            api_client.post(_decline_url(inv.token))  # anonymous → 401/403
        assert inv.token not in caplog.text
        for record in caplog.records:
            assert inv.token not in record.getMessage()
