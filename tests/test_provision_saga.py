"""Provisioning is a saga, and a login grant is single-use (WORK-03).

Two ways this module used to leak control of things it had already paid
for or already handed out.

**Provisioning.** Billing was charged, then auth was asked for an account,
then a membership was written — three services, no shared transaction and
no record of where an attempt got to. A failure after the charge left an
orphan charge nobody could find; a failure after the auth call left an
account with no membership; and a retry was a brand-new attempt that paid
again. The saga gives every attempt a stable operation id and a row that
says which of those things happened.

**Login grants.** The claim endpoint minted an auth login grant for an
invited address with no account yet. The grant is single-use in auth — but
nothing stopped the same invite token from minting the next one, and the
next: one leaked invitation link was an unbounded supply of session-bearing
credentials for that mailbox. One live grant at a time now, with the window
reopening after its TTL so a real "the email never arrived" still works.
"""

import pytest
from django.utils import timezone

from stapel_core.comm.exceptions import FunctionCallError
from stapel_core.comm.registry import function_registry
from stapel_core.verification import grant_verification

from stapel_workspaces import entitlements, services
from stapel_workspaces.errors import ERR_429_INVITATION_GRANT_PENDING
from stapel_workspaces.models import (
    ProvisionState,
    Role,
    WorkspaceMember,
    WorkspaceProvisionOperation,
)

BASE = "/workspaces/api/workspaces/v1"


@pytest.fixture
def auth_provision(db):
    """Scripted ``auth.provision_user``; ``response`` steers the answer."""
    state = {
        "calls": [],
        "response": None,
        "next_user": None,
        "raise_exc": None,
    }

    def provider(payload):
        state["calls"].append(payload)
        if state["raise_exc"]:
            raise state["raise_exc"]
        if state["response"] is not None:
            return state["response"]
        return {
            "user_id": str(state["next_user"]),
            "generated_password": "srv-generated-1",
        }

    function_registry.register(services.PROVISION_USER, provider)
    yield state
    function_registry._providers.pop(services.PROVISION_USER, None)
    function_registry._schemas.pop(services.PROVISION_USER, None)


@pytest.fixture
def billing():
    """Scripted ``billing.debit`` + ``billing.credit`` (the refund seam)."""
    state = {"debits": [], "credits": [], "debit_ok": True, "credit_ok": True}

    def debit(payload):
        state["debits"].append(payload)
        return {"ok": state["debit_ok"], "reason": "insufficient_credits"}

    def credit(payload):
        state["credits"].append(payload)
        return {"ok": state["credit_ok"]}

    function_registry.register(entitlements.DEBIT, debit)
    function_registry.register(entitlements.CREDIT, credit)
    yield state
    for name in (entitlements.DEBIT, entitlements.CREDIT):
        function_registry._providers.pop(name, None)
        function_registry._schemas.pop(name, None)


@pytest.mark.django_db
class TestTheSagaIsIdempotent:
    def test_a_replay_does_not_charge_or_provision_twice(
        self, user, other_user, auth_provision, billing, settings
    ):
        settings.STAPEL_WORKSPACES = {"PROVISION_USER_CREDITS": 5}
        ws = services.create_workspace(user=user, name="Acme")
        auth_provision["next_user"] = other_user.pk

        first, username, password = services.provision_member(
            workspace=ws, username_local="jdoe", role=Role.MEMBER,
            provisioned_by=user,
        )
        again, username_again, password_again = services.provision_member(
            workspace=ws, username_local="jdoe", role=Role.MEMBER,
            provisioned_by=user,
        )

        assert again.pk == first.pk
        assert username_again == username
        assert len(billing["debits"]) == 1, "the replay paid a second time"
        assert len(auth_provision["calls"]) == 1
        # The password was handed over once; a replay cannot re-issue it.
        assert password == "srv-generated-1"
        assert password_again is None
        assert WorkspaceProvisionOperation.objects.count() == 1

    def test_a_retry_after_a_failure_costs_exactly_one_provisioning(
        self, user, other_user, auth_provision, billing, settings
    ):
        """The money statement: one account, one net charge, one operation.

        The first attempt dies at auth and is refunded; the retry pays for
        the account it actually gets. Before the saga the first charge
        stood forever and the retry paid again, so an org that pressed the
        button twice through a wobbly auth paid twice for one user.
        """
        settings.STAPEL_WORKSPACES = {"PROVISION_USER_CREDITS": 5}
        ws = services.create_workspace(user=user, name="Acme")
        auth_provision["raise_exc"] = RuntimeError("auth exploded")

        with pytest.raises(FunctionCallError):
            services.provision_member(
                workspace=ws, username_local="jdoe", role=Role.MEMBER,
                provisioned_by=user,
            )
        auth_provision["raise_exc"] = None
        auth_provision["next_user"] = other_user.pk
        services.provision_member(
            workspace=ws, username_local="jdoe", role=Role.MEMBER,
            provisioned_by=user,
        )

        charged = sum(d["credits"] for d in billing["debits"])
        refunded = sum(c["credits"] for c in billing["credits"])
        assert charged - refunded == 5
        assert len({d["idempotency_key"] for d in billing["debits"]}) == 2, (
            "a genuine retry must not be deduped away as a redelivery"
        )
        assert WorkspaceProvisionOperation.objects.count() == 1
        assert WorkspaceMember.objects.filter(
            workspace=ws, provisioned=True
        ).count() == 1

    def test_a_redelivery_of_one_attempt_charges_once(
        self, user, other_user, auth_provision, billing, settings, monkeypatch
    ):
        """The idempotency key names the ATTEMPT, so comm can redeliver it."""
        settings.STAPEL_WORKSPACES = {"PROVISION_USER_CREDITS": 5}
        ws = services.create_workspace(user=user, name="Acme")
        auth_provision["next_user"] = other_user.pk
        services.provision_member(
            workspace=ws, username_local="jdoe", role=Role.MEMBER,
            provisioned_by=user,
        )
        key = billing["debits"][0]["idempotency_key"]

        services.provision_member(
            workspace=ws, username_local="jdoe", role=Role.MEMBER,
            provisioned_by=user,
        )
        assert [d["idempotency_key"] for d in billing["debits"]] == [key]


@pytest.mark.django_db
class TestFailureCompensates:
    def test_an_auth_failure_refunds_the_charge(
        self, user, auth_provision, billing, settings
    ):
        settings.STAPEL_WORKSPACES = {"PROVISION_USER_CREDITS": 5}
        ws = services.create_workspace(user=user, name="Acme")
        auth_provision["response"] = {"error": "error.409.username_taken"}

        with pytest.raises(services.ProvisionError):
            services.provision_member(
                workspace=ws, username_local="jdoe", role=Role.MEMBER,
                provisioned_by=user,
            )

        (refund,) = billing["credits"]
        assert refund["credits"] == 5
        operation = WorkspaceProvisionOperation.objects.get()
        assert operation.state == ProvisionState.COMPENSATED
        assert operation.credits_to_refund == 0
        assert not WorkspaceMember.objects.filter(
            workspace=ws, provisioned=True
        ).exists()

    def test_an_unrefundable_charge_becomes_a_queue_not_a_log_line(
        self, user, auth_provision, billing, settings
    ):
        """Billing publishes no refund Function today — the debt is recorded."""
        settings.STAPEL_WORKSPACES = {"PROVISION_USER_CREDITS": 5}
        ws = services.create_workspace(user=user, name="Acme")
        auth_provision["response"] = {"error": "error.409.username_taken"}
        billing["credit_ok"] = False

        with pytest.raises(services.ProvisionError):
            services.provision_member(
                workspace=ws, username_local="jdoe", role=Role.MEMBER,
                provisioned_by=user,
            )

        operation = WorkspaceProvisionOperation.objects.get()
        assert operation.state == ProvisionState.COMPENSATING
        assert operation.credits_to_refund == 5
        assert operation.last_error

        billing["credit_ok"] = True
        (settled,) = services.reconcile_provision_operations()
        assert settled.state == ProvisionState.COMPENSATED
        assert settled.credits_to_refund == 0
        # Settled rows leave the queue: reconciling again is a no-op.
        assert services.reconcile_provision_operations() == []

    def test_an_orphan_account_is_named_on_the_row(
        self, user, other_user, auth_provision, billing, settings, monkeypatch
    ):
        """The membership step failed after auth had already minted an account."""
        settings.STAPEL_WORKSPACES = {"PROVISION_USER_CREDITS": 5}
        ws = services.create_workspace(user=user, name="Acme")
        auth_provision["next_user"] = other_user.pk

        def boom(**kwargs):
            raise RuntimeError("membership write failed")

        monkeypatch.setattr(services, "_complete_provision", boom)
        with pytest.raises(RuntimeError):
            services.provision_member(
                workspace=ws, username_local="jdoe", role=Role.MEMBER,
                provisioned_by=user,
            )

        operation = WorkspaceProvisionOperation.objects.get()
        assert operation.user_id == other_user.pk, (
            "the account auth minted must be findable"
        )
        assert operation.credits_to_refund == 0
        assert operation.state == ProvisionState.COMPENSATED

        # And the retry finishes that account instead of asking auth for a
        # second one under a username auth would refuse anyway.
        monkeypatch.undo()
        auth_calls_before = len(auth_provision["calls"])
        member, _, password = services.provision_member(
            workspace=ws, username_local="jdoe", role=Role.MEMBER,
            provisioned_by=user,
        )
        assert member.user_id == other_user.pk
        assert password is None
        assert len(auth_provision["calls"]) == auth_calls_before

    def test_the_command_reports_what_is_owed(
        self, user, auth_provision, billing, settings
    ):
        from io import StringIO

        from django.core.management import call_command

        settings.STAPEL_WORKSPACES = {"PROVISION_USER_CREDITS": 5}
        ws = services.create_workspace(user=user, name="Acme")
        auth_provision["response"] = {"error": "error.409.username_taken"}
        billing["credit_ok"] = False
        with pytest.raises(services.ProvisionError):
            services.provision_member(
                workspace=ws, username_local="jdoe", role=Role.MEMBER,
                provisioned_by=user,
            )

        billing["credit_ok"] = True
        out = StringIO()
        call_command("reconcile_provisioning", stdout=out)
        assert "settled 1; 0 still owed" in out.getvalue()


@pytest.mark.django_db
class TestTheLoginGrantIsSingleUse:
    def _claim(self, api_client, invitation):
        return api_client.post(f"{BASE}/invitations/{invitation.token}/claim")

    @pytest.fixture
    def grants(self):
        state = {"calls": []}

        def provider(payload):
            state["calls"].append(payload)
            return {"grant_token": f"grant-{len(state['calls'])}", "created": True}

        function_registry.register(services.ISSUE_LOGIN_GRANT, provider)
        yield state
        function_registry._providers.pop(services.ISSUE_LOGIN_GRANT, None)
        function_registry._schemas.pop(services.ISSUE_LOGIN_GRANT, None)

    def test_a_second_claim_inside_the_window_is_refused(
        self, api_client, user, grants
    ):
        """The finding: one invite link, an endless supply of sign-ins."""
        ws = services.create_workspace(user=user, name="Acme")
        inv = services.create_invitation(
            workspace=ws, email="newbie@example.com", role=Role.MEMBER,
            invited_by=user,
        )

        first = self._claim(api_client, inv)
        assert first.status_code == 200, first.content
        assert first.json()["grant_token"] == "grant-1"

        second = self._claim(api_client, inv)
        assert second.status_code == 429
        assert second.json()["localizable_error"] == ERR_429_INVITATION_GRANT_PENDING
        assert int(second["Retry-After"]) > 0
        assert len(grants["calls"]) == 1

    def test_the_window_reopens_so_a_lost_email_is_recoverable(
        self, api_client, user, grants
    ):
        ws = services.create_workspace(user=user, name="Acme")
        inv = services.create_invitation(
            workspace=ws, email="newbie@example.com", role=Role.MEMBER,
            invited_by=user,
        )
        assert self._claim(api_client, inv).status_code == 200

        inv.refresh_from_db()
        inv.login_grant_issued_at = timezone.now() - timezone.timedelta(seconds=901)
        inv.save(update_fields=["login_grant_issued_at"])

        assert self._claim(api_client, inv).status_code == 200
        assert len(grants["calls"]) == 2
        inv.refresh_from_db()
        assert inv.login_grant_count == 2

    def test_two_simultaneous_claims_mint_one_grant(self, user, grants):
        """The claim is a conditional UPDATE, not a read then a write.

        Reproduced the way this suite reproduces every such window: the
        competing claim commits between the caller's read of the invitation
        and its own attempt to take the slot.
        """
        ws = services.create_workspace(user=user, name="Acme")
        inv = services.create_invitation(
            workspace=ws, email="newbie@example.com", role=Role.MEMBER,
            invited_by=user,
        )
        stale = type(inv).objects.get(pk=inv.pk)

        services.issue_invitation_login_grant(invitation=inv)
        with pytest.raises(services.LoginGrantAlreadyIssued):
            services.issue_invitation_login_grant(invitation=stale)

        assert len(grants["calls"]) == 1

    def test_an_auth_failure_gives_the_window_back(self, user):
        """A grant that was never minted must not spend the invitee's slot."""
        ws = services.create_workspace(user=user, name="Acme")
        inv = services.create_invitation(
            workspace=ws, email="newbie@example.com", role=Role.MEMBER,
            invited_by=user,
        )

        def failing(payload):
            raise FunctionCallError("auth is down")

        function_registry.register(services.ISSUE_LOGIN_GRANT, failing)
        try:
            with pytest.raises(FunctionCallError):
                services.issue_invitation_login_grant(invitation=inv)
        finally:
            function_registry._providers.pop(services.ISSUE_LOGIN_GRANT, None)

        inv.refresh_from_db()
        assert inv.login_grant_issued_at is None
        assert inv.login_grant_count == 0

    def test_a_deployment_can_switch_the_window_off(
        self, api_client, user, grants, settings
    ):
        settings.STAPEL_WORKSPACES = {"INVITATION_LOGIN_GRANT_TTL_SECONDS": 0}
        ws = services.create_workspace(user=user, name="Acme")
        inv = services.create_invitation(
            workspace=ws, email="newbie@example.com", role=Role.MEMBER,
            invited_by=user,
        )
        assert self._claim(api_client, inv).status_code == 200
        assert self._claim(api_client, inv).status_code == 200
        assert len(grants["calls"]) == 2


@pytest.mark.django_db
class TestTheApiPath:
    def test_provisioning_through_the_endpoint_records_the_operation(
        self, authed_client, user, other_user, auth_provision
    ):
        ws = services.create_workspace(user=user, name="Acme")
        auth_provision["next_user"] = other_user.pk
        grant_verification(user_id=str(user.pk), scope="sensitive", max_age=300)

        resp = authed_client.post(
            f"{BASE}/{ws.id}/members/provision",
            {"username_local": "jdoe", "role": "member"},
            format="json",
        )

        assert resp.status_code == 201, resp.content
        operation = WorkspaceProvisionOperation.objects.get(workspace=ws)
        assert operation.state == ProvisionState.COMPLETED
        assert operation.user_id == other_user.pk
        assert operation.username == f"{ws.slug}/jdoe"
