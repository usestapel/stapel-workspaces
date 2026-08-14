"""Billing entitlement seam tests (org-program spec §D2).

Billing is NOT installed in this suite, so conftest stands in a provider
that allows; enforcement is exercised by displacing it with a scripted one
(the real provider lives in stapel-billing; only its payload contract is
shared), and the closed path by removing it entirely (``no_billing``).
"""

import pytest
from django.test import override_settings
from django.utils import timezone

from stapel_core.comm.registry import function_registry

from stapel_workspaces import entitlements
from stapel_workspaces.errors import (
    ERR_402_ENTITLEMENT_REQUIRED,
    ERR_402_MEMBER_LIMIT_REACHED,
    ERR_503_BILLING_UNAVAILABLE,
)
from stapel_workspaces.models import Role, WorkspaceInvitation, WorkspaceMember

BASE = "/workspaces/api/workspaces/v1"


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

    # Displaces the suite-wide allow-everything provider (conftest): a
    # function name has exactly one provider, and this one is scripted.
    function_registry._providers.pop(entitlements.CHECK_ENTITLEMENT, None)
    function_registry.register(entitlements.CHECK_ENTITLEMENT, provider)
    yield state
    function_registry._providers.pop(entitlements.CHECK_ENTITLEMENT, None)
    function_registry._schemas.pop(entitlements.CHECK_ENTITLEMENT, None)


@pytest.fixture
def no_billing():
    """No provider answers ``billing.check_entitlement`` — the seam is down.

    Indistinguishable, at this seam, from a billing that crashed, scaled to
    zero or lost its FUNCTION_ROUTES entry: all of them arrive as the same
    two comm wiring errors.
    """
    from stapel_workspaces import entitlements as ent

    function_registry._providers.pop(ent.CHECK_ENTITLEMENT, None)
    function_registry._schemas.pop(ent.CHECK_ENTITLEMENT, None)
    yield


def _settings(**overrides):
    """STAPEL_WORKSPACES with only the keys under test replaced."""
    from django.conf import settings

    base = dict(getattr(settings, "STAPEL_WORKSPACES", {}) or {})
    base.update(overrides)
    return base


@pytest.mark.django_db
class TestSeamFailsClosed:
    """An unreachable billing refuses; it does not hand out unlimited plan."""

    def test_check_entitlement_refuses(self, user, no_billing):
        with pytest.raises(entitlements.BillingUnavailable):
            entitlements.check_entitlement(user.pk, entitlements.ENT_ORG)

    def test_debit_refuses(self, user, no_billing):
        ws = _create_ws(user)
        with pytest.raises(entitlements.BillingUnavailable):
            entitlements.debit_provision_credits(
                ws, provision_id="p-1", username="someone", credits=5
            )

    def test_work_workspace_creation_answers_503(self, authed_client, no_billing):
        resp = authed_client.post(
            f"{BASE}/", {"name": "Org", "type": "work"}, format="json"
        )
        assert resp.status_code == 503, resp.content
        assert resp.json()["localizable_error"] == ERR_503_BILLING_UNAVAILABLE

    def test_invite_answers_503(self, authed_client, user, other_user, no_billing):
        ws = _create_ws(user)
        resp = authed_client.post(
            f"{BASE}/{ws.id}/members/invite",
            {"emails": [other_user.email], "role": "member"},
            format="json",
        )
        assert resp.status_code == 503, resp.content
        assert resp.json()["localizable_error"] == ERR_503_BILLING_UNAVAILABLE


@pytest.mark.django_db
class TestAllowUnbilled:
    """The one deployment that really sells nothing says so explicitly."""

    def test_check_entitlement_allows(self, user, no_billing):
        with override_settings(STAPEL_WORKSPACES=_settings(ALLOW_UNBILLED=True)):
            result = entitlements.check_entitlement(user.pk, entitlements.ENT_ORG)
        assert result.allowed is True
        assert result.reason == "billing_not_installed"

    def test_work_workspace_creation_unrestricted(self, authed_client, no_billing):
        with override_settings(STAPEL_WORKSPACES=_settings(ALLOW_UNBILLED=True)):
            resp = authed_client.post(
                f"{BASE}/", {"name": "Org", "type": "work"}, format="json"
            )
        assert resp.status_code == 201, resp.content

    def test_debit_is_a_no_op(self, user, no_billing):
        ws = _create_ws(user)
        with override_settings(STAPEL_WORKSPACES=_settings(ALLOW_UNBILLED=True)):
            entitlements.debit_provision_credits(
                ws, provision_id="p-1", username="someone", credits=5
            )

    @pytest.mark.parametrize("spelling", ["false", "1", "true"])
    def test_the_environment_cannot_open_it(
        self, user, no_billing, monkeypatch, spelling
    ):
        """No env spelling opens the paywall — the key is ``no_env``.

        Both halves matter. A stray ``ALLOW_UNBILLED=1`` in a shared pod
        must not hand an outage unlimited plan; and ``=false``, which an
        operator would write to DISABLE this, must not be read as True the
        way ``bool("false")`` would.
        """
        monkeypatch.setenv("ALLOW_UNBILLED", spelling)
        with pytest.raises(entitlements.BillingUnavailable):
            entitlements.check_entitlement(user.pk, entitlements.ENT_ORG)


class TestBillingSeamCheck:
    """E011: a closed seam nobody wired is a deploy-time failure, not a 503.

    "Wired" is a question about the transport this deployment runs, not about
    one transport's route table: ``FUNCTION_ROUTES`` exists only for the http
    transport (``stapel_core.comm.config``), so reading it under nats asked
    the wrong witness and blocked the boot of a correctly wired fleet.
    """

    def test_error_when_no_provider_in_process(self, no_billing):
        from stapel_workspaces.checks import check_billing_seam_wired

        assert [e.id for e in check_billing_seam_wired(None)] == [
            "stapel_workspaces.E011"
        ]

    def test_silent_when_a_provider_is_registered_in_process(self):
        """The monolith: billing's ``ready()`` published the Function here."""
        from stapel_workspaces.checks import check_billing_seam_wired

        # The autouse ``billing_seam`` fixture is that provider.
        assert check_billing_seam_wired(None) == []

    def test_silent_when_declared_unbilled(self, no_billing):
        from stapel_workspaces.checks import check_billing_seam_wired

        with override_settings(STAPEL_WORKSPACES=_settings(ALLOW_UNBILLED=True)):
            assert check_billing_seam_wired(None) == []

    def test_silent_when_routed(self, no_billing):
        from stapel_workspaces.checks import check_billing_seam_wired

        with override_settings(STAPEL_COMM={
            "FUNCTION_TRANSPORT": "http",
            "FUNCTION_ROUTES": {"billing.": "http://b"},
        }):
            assert check_billing_seam_wired(None) == []

    def test_error_when_http_transport_has_no_billing_route(self):
        """The genuinely unwired http deployment still fails, provider or not.

        With ``FUNCTION_TRANSPORT="http"`` ``call()`` never consults the
        registry (``stapel_core.comm.functions.call``), so a locally
        registered provider — the autouse fixture here — does not make the
        seam reachable. Only a matching route does.
        """
        from stapel_workspaces.checks import check_billing_seam_wired

        with override_settings(STAPEL_COMM={
            "FUNCTION_TRANSPORT": "http",
            "FUNCTION_ROUTES": {"cdn.": "http://cdn"},
        }):
            assert [e.id for e in check_billing_seam_wired(None)] == [
                "stapel_workspaces.E011"
            ]

    def test_silent_under_nats_without_any_routes(self, no_billing):
        """NATS needs no route table: the subject IS the function name.

        A split fleet that serves billing with ``manage.py serve_functions``
        over NATS is wired; E011 used to fire on it and refuse the boot.
        """
        from stapel_workspaces.checks import check_billing_seam_wired

        with override_settings(STAPEL_COMM={"FUNCTION_TRANSPORT": "nats"}):
            assert check_billing_seam_wired(None) == []

    def test_silent_under_a_custom_transport(self, no_billing):
        """A dotted-path transport does its own addressing (gRPC, a mesh...)."""
        from stapel_workspaces.checks import check_billing_seam_wired

        with override_settings(STAPEL_COMM={
            "FUNCTION_TRANSPORT": "acme.transport.call",
        }):
            assert check_billing_seam_wired(None) == []

    def test_error_when_the_transport_cannot_dispatch_at_all(self):
        """An unknown transport: every entitlement call raises, so 503s follow."""
        from stapel_workspaces.checks import check_billing_seam_wired

        with override_settings(STAPEL_COMM={"FUNCTION_TRANSPORT": "kafka"}):
            assert [e.id for e in check_billing_seam_wired(None)] == [
                "stapel_workspaces.E011"
            ]


class TestProfilesNameWriteCheck:
    """W001 reads the same evidence as E011, and used to read it the same way."""

    def test_silent_under_nats_without_any_routes(self):
        from stapel_workspaces.checks import check_profiles_name_write_wired

        with override_settings(STAPEL_COMM={"FUNCTION_TRANSPORT": "nats"}):
            assert check_profiles_name_write_wired(None) == []

    def test_warns_when_http_has_no_profiles_route(self):
        from stapel_workspaces.checks import check_profiles_name_write_wired

        with override_settings(STAPEL_COMM={
            "FUNCTION_TRANSPORT": "http",
            "FUNCTION_ROUTES": {"cdn.": "http://cdn"},
        }):
            assert [w.id for w in check_profiles_name_write_wired(None)] == [
                "stapel_workspaces.W001"
            ]


@pytest.mark.django_db
class TestOrgEntitlement:
    def test_denied_org_creation_402(self, authed_client, fake_billing):
        fake_billing["response"] = {"allowed": False, "reason": "plan"}
        resp = authed_client.post(
            f"{BASE}/", {"name": "Org", "type": "work"}, format="json"
        )
        assert resp.status_code == 402
        assert resp.json()["localizable_error"] == ERR_402_ENTITLEMENT_REQUIRED
        assert fake_billing["calls"][-1]["key"] == entitlements.ENT_ORG

    def test_personal_workspace_never_gated(self, authed_client, fake_billing):
        fake_billing["response"] = {"allowed": False}
        resp = authed_client.post(
            f"{BASE}/", {"name": "Me", "type": "personal"}, format="json"
        )
        assert resp.status_code == 201, resp.content
        assert fake_billing["calls"] == []

    def test_allowed_org_creation_passes_owner_anchor(
        self, authed_client, user, fake_billing
    ):
        resp = authed_client.post(
            f"{BASE}/", {"name": "Org", "type": "work"}, format="json"
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
