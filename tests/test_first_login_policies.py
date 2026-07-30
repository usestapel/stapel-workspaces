"""The org's first-login demands are independent checkboxes (#90).

What was inert. ``Workspace.settings["security"]["provisioned_user_policy"]``
was ONE string, handed to ``auth.provision_user`` as ``first_login_policy``,
and auth spelled the account creation ``password_change_required=(policy ==
"password_change"), mfa_enrollment_required=(policy == "mfa_enroll")``. Both
boxes ticked in the invite modal could not be expressed anywhere along that
path: naming either demand cleared the other. The frontend's checkboxes were
inert for exactly that reason, not because anybody forgot to wire them.

Two halves are pinned here.

**Provisioning takes a set.** ``provision_member`` sends
``first_login_policies`` — every configured demand, together — and the
workspace-settings parser accepts either spelling (the plural list, or the
pre-0.13 singular string, so a row written by an older release keeps its
meaning without a data migration).

**Acceptance applies them too**, which is the new reach: an invited account
is hardened when it joins, through ``auth.apply_first_login_policies``. Two
properties of that seam are load-bearing and both are tested:

* it is **not called at all** unless the org configured policies — so the
  default (``password_change``, which exists for accounts the org MINTED
  and whose password somebody else chose) never forces a password rotation
  on a new hire who joined with their own account, and an org that never
  opened the security screen is not coupled to auth's version;
* when the org DID configure them and auth cannot honour them, the
  acceptance **fails** (503 / the keyed error) and the membership rolls
  back. An org that states a precondition for admission does not get a
  member who skipped it. Best-effort here is how a security control
  silently stops running.

Why any of this is no longer decorative: stapel-auth 0.15.0 moved the
first-login gate into ``_issue_session_tokens``, the single minter all 19
session paths funnel through, defaulting to every path. A raised flag now
blocks admission everywhere, not only on the password form.
"""

import pytest
from stapel_core.comm.exceptions import FunctionNotRegistered
from stapel_core.comm.registry import function_registry
from stapel_core.verification import grant_verification

from stapel_workspaces import services
from stapel_workspaces.dto import WorkspaceSecuritySettings
from stapel_workspaces.errors import ERR_503_AUTH_UNAVAILABLE
from stapel_workspaces.models import Role, WorkspaceMember

BASE = "/workspaces/api/workspaces/v1"
ACCEPT = f"{BASE}/invitations/accept"
BOTH = ["password_change", "mfa_enroll"]


class Recorder(list):
    """Payloads a scripted comm provider was sent, plus a scripted reply."""

    result = None


def _register(name, provider):
    """Register a provider, replacing any leftover from a previous test."""
    function_registry._providers.pop(name, None)
    function_registry._schemas.pop(name, None)
    function_registry.register(name, provider)


@pytest.fixture
def fake_provision_user(db):
    """Scripted ``auth.provision_user``; records the payloads it was sent.

    Creates a REAL user row: ``provision_member`` writes a membership FK
    against whatever id auth answers with, so a fabricated UUID would only
    surface as a constraint error at teardown.
    """
    from stapel_core.django.users.models import User

    calls = Recorder()

    def provider(payload):
        calls.append(payload)
        created = User.objects.create_user(
            username=payload["username"], password="provisioned-pass-1234"
        )
        return {"user_id": str(created.pk), "generated_password": "generated-xyz"}

    _register(services.PROVISION_USER, provider)
    yield calls
    function_registry._providers.pop(services.PROVISION_USER, None)
    function_registry._schemas.pop(services.PROVISION_USER, None)


@pytest.fixture
def fake_apply_policies():
    """Scripted ``auth.apply_first_login_policies``; records the payloads."""
    calls = Recorder()

    def provider(payload):
        calls.append(payload)
        if calls.result is not None:
            return calls.result
        return {"applied": list(payload["policies"])}

    _register(services.APPLY_FIRST_LOGIN_POLICIES, provider)
    yield calls
    function_registry._providers.pop(services.APPLY_FIRST_LOGIN_POLICIES, None)
    function_registry._schemas.pop(services.APPLY_FIRST_LOGIN_POLICIES, None)


@pytest.fixture
def no_auth_policy_seam():
    """The deployment runs an older auth: the Function is not registered."""
    function_registry._providers.pop(services.APPLY_FIRST_LOGIN_POLICIES, None)
    function_registry._schemas.pop(services.APPLY_FIRST_LOGIN_POLICIES, None)
    yield


@pytest.fixture
def sensitive_grant(user):
    """Seed a fresh HIGH step-up grant (scope ``sensitive``) for *user*.

    The security block of the workspace PATCH is a HIGH surface
    (``workspace.security.manage`` + ``@requires_verification``), same as
    provisioning.
    """
    grant_verification(user_id=str(user.pk), scope="sensitive", max_age=300)


def _ws(user, security=None):
    ws = services.create_workspace(user=user, name="Acme")
    if security is not None:
        ws.settings = {"security": security}
        ws.save(update_fields=["settings"])
    return ws


def _invite(ws, email, inviter):
    return services.create_invitation(
        workspace=ws, email=email, role=Role.MEMBER, invited_by=inviter
    )


# ---------------------------------------------------------------------------
# The settings block
# ---------------------------------------------------------------------------


class TestSecuritySettingsParsing:
    def test_both_policies_survive_the_parse(self):
        parsed = WorkspaceSecuritySettings.from_settings(
            {"security": {"provisioned_user_policies": BOTH}}
        )
        assert parsed.provisioned_user_policies == BOTH
        assert parsed.policies_configured is True

    def test_order_is_canonical_not_the_caller_s(self):
        """Two orgs asking for the same thing produce the same payload."""
        parsed = WorkspaceSecuritySettings.from_settings(
            {"security": {"provisioned_user_policies": ["mfa_enroll", "password_change"]}}
        )
        assert parsed.provisioned_user_policies == BOTH

    def test_pre_0_13_single_string_still_understood(self):
        """A workspace row written by an older release keeps its meaning.

        No data migration: the old spelling is read, not rewritten.
        """
        parsed = WorkspaceSecuritySettings.from_settings(
            {"security": {"provisioned_user_policy": "mfa_enroll"}}
        )
        assert parsed.provisioned_user_policies == ["mfa_enroll"]
        assert parsed.policies_configured is True

    def test_default_is_the_historical_password_change(self):
        parsed = WorkspaceSecuritySettings.from_settings({})
        assert parsed.provisioned_user_policies == ["password_change"]
        assert parsed.policies_configured is False

    def test_explicit_empty_list_is_a_configured_none(self):
        parsed = WorkspaceSecuritySettings.from_settings(
            {"security": {"provisioned_user_policies": []}}
        )
        assert parsed.provisioned_user_policies == []
        assert parsed.policies_configured is True

    def test_garbage_members_are_dropped_not_fatal(self):
        """This parse runs on every provision and every accept.

        A typo in one workspace's JSON blob must not take that org's
        invitations down.
        """
        parsed = WorkspaceSecuritySettings.from_settings(
            {"security": {"provisioned_user_policies": ["mfa_enroll", "sudo"]}}
        )
        assert parsed.provisioned_user_policies == ["mfa_enroll"]

    def test_the_default_never_reaches_an_invited_account(self):
        """The whole reason ``policies_configured`` exists.

        ``password_change`` is the right default for an account the ORG
        minted — somebody other than its owner chose the password, so it
        must stop working. Imposing it on a person who joined with their
        own account would force a rotation on every new hire of every org
        that never opened the security screen.
        """
        default = WorkspaceSecuritySettings.from_settings({})
        assert default.provisioned_user_policies == ["password_change"]
        assert default.policies_for_invited_members() == []

        configured = WorkspaceSecuritySettings.from_settings(
            {"security": {"provisioned_user_policies": BOTH}}
        )
        assert configured.policies_for_invited_members() == BOTH


# ---------------------------------------------------------------------------
# Provisioning
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestProvisioningSendsTheWholeSet:
    def test_both_policies_reach_auth_together(self, user, fake_provision_user):
        """The payload that could not be written before this change."""
        ws = _ws(user, {"provisioned_user_policies": BOTH})
        services.provision_member(
            workspace=ws, username_local="jdoe", role=Role.MEMBER, provisioned_by=user
        )
        assert fake_provision_user[0]["first_login_policies"] == BOTH
        assert "first_login_policy" not in fake_provision_user[0]

    def test_configured_none_is_sent_as_an_empty_set(self, user, fake_provision_user):
        ws = _ws(user, {"provisioned_user_policies": []})
        services.provision_member(
            workspace=ws, username_local="jdoe", role=Role.MEMBER, provisioned_by=user
        )
        assert fake_provision_user[0]["first_login_policies"] == []

    def test_unconfigured_org_still_forces_the_password_change(
        self, user, fake_provision_user
    ):
        """The historical default holds where it belongs.

        An org-minted account's password was chosen by the admin; it has
        to stop working at first login whether or not anybody configured
        anything.
        """
        ws = _ws(user)
        services.provision_member(
            workspace=ws, username_local="jdoe", role=Role.MEMBER, provisioned_by=user
        )
        assert fake_provision_user[0]["first_login_policies"] == ["password_change"]

    def test_pre_0_13_settings_row_still_provisions(self, user, fake_provision_user):
        ws = _ws(user, {"provisioned_user_policy": "mfa_enroll"})
        services.provision_member(
            workspace=ws, username_local="jdoe", role=Role.MEMBER, provisioned_by=user
        )
        assert fake_provision_user[0]["first_login_policies"] == ["mfa_enroll"]


# ---------------------------------------------------------------------------
# Invitation acceptance — the new reach
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestAcceptAppliesThePolicies:
    def test_accepting_hardens_the_joining_account(
        self, api_client, user, other_user, fake_apply_policies
    ):
        ws = _ws(user, {"provisioned_user_policies": BOTH})
        inv = _invite(ws, other_user.email, user)
        api_client.force_authenticate(user=other_user)
        resp = api_client.post(ACCEPT, {"token": inv.token}, format="json")
        assert resp.status_code == 200, resp.content
        assert fake_apply_policies == [
            {"user_id": str(other_user.id), "policies": BOTH}
        ]
        assert WorkspaceMember.objects.filter(workspace=ws, user=other_user).exists()

    def test_unconfigured_org_never_touches_the_seam(
        self, api_client, user, other_user, fake_apply_policies
    ):
        """No call at all — not "a call with an empty set".

        This is what keeps an org that never opened the security screen
        from being coupled to auth's version, and what keeps the
        provisioning default off an invited account.
        """
        ws = _ws(user)
        inv = _invite(ws, other_user.email, user)
        api_client.force_authenticate(user=other_user)
        assert api_client.post(
            ACCEPT, {"token": inv.token}, format="json"
        ).status_code == 200
        assert fake_apply_policies == []

    def test_configured_empty_set_also_skips_the_call(
        self, api_client, user, other_user, fake_apply_policies
    ):
        ws = _ws(user, {"provisioned_user_policies": []})
        inv = _invite(ws, other_user.email, user)
        api_client.force_authenticate(user=other_user)
        assert api_client.post(
            ACCEPT, {"token": inv.token}, format="json"
        ).status_code == 200
        assert fake_apply_policies == []

    def test_auth_unavailable_refuses_the_admission(
        self, api_client, user, other_user, no_auth_policy_seam
    ):
        """The org stated a precondition; a seam that cannot honour it must
        refuse, not admit an unhardened member.

        ``auth.apply_first_login_policies`` is simply not registered here —
        the deployment ran an older auth, or the route is unwired.
        """
        ws = _ws(user, {"provisioned_user_policies": BOTH})
        inv = _invite(ws, other_user.email, user)
        api_client.force_authenticate(user=other_user)
        resp = api_client.post(ACCEPT, {"token": inv.token}, format="json")
        assert resp.status_code == 503
        assert resp.json()["localizable_error"] == ERR_503_AUTH_UNAVAILABLE

    def test_a_refused_acceptance_leaves_nothing_behind(
        self, api_client, user, other_user, no_auth_policy_seam
    ):
        """The whole acceptance rolls back — no membership, no consumed
        invitation, so a retry once auth is back still works."""
        ws = _ws(user, {"provisioned_user_policies": BOTH})
        inv = _invite(ws, other_user.email, user)
        api_client.force_authenticate(user=other_user)
        api_client.post(ACCEPT, {"token": inv.token}, format="json")
        assert not WorkspaceMember.objects.filter(
            workspace=ws, user=other_user
        ).exists()
        inv.refresh_from_db()
        assert inv.accepted_at is None

    def test_structured_auth_failure_passes_through_keyed(
        self, api_client, user, other_user, fake_apply_policies
    ):
        fake_apply_policies.result = {"error": "error.404.not_found"}
        ws = _ws(user, {"provisioned_user_policies": BOTH})
        inv = _invite(ws, other_user.email, user)
        api_client.force_authenticate(user=other_user)
        resp = api_client.post(ACCEPT, {"token": inv.token}, format="json")
        assert resp.status_code == 404
        assert resp.json()["localizable_error"] == "error.404.not_found"
        assert not WorkspaceMember.objects.filter(
            workspace=ws, user=other_user
        ).exists()

    def test_service_helper_makes_no_call_for_an_empty_set(
        self, db, user, no_auth_policy_seam
    ):
        """Pinned at the service too: `apply_first_login_policies([])` must
        not even look for the Function, or every deployment inherits the
        coupling regardless of the view path taken."""
        assert services.apply_first_login_policies(user_id=user.id, policies=[]) == []
        with pytest.raises(FunctionNotRegistered):
            services.apply_first_login_policies(
                user_id=user.id, policies=["mfa_enroll"]
            )


# ---------------------------------------------------------------------------
# The settings PATCH surface
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestSecurityPatchAcceptsTheSet:
    def _patch(self, client, ws, security):
        return client.patch(
            f"{BASE}/{ws.id}",
            {"settings": {"security": security}},
            format="json",
        )

    def test_both_boxes_can_be_ticked(self, authed_client, user, sensitive_grant):
        ws = services.create_workspace(user=user, name="Acme")
        resp = self._patch(
            authed_client, ws, {"provisioned_user_policies": BOTH}
        )
        assert resp.status_code == 200, resp.content
        ws.refresh_from_db()
        assert (
            services.security_settings_for(ws).provisioned_user_policies == BOTH
        )

    def test_unknown_policy_is_rejected(self, authed_client, user, sensitive_grant):
        ws = services.create_workspace(user=user, name="Acme")
        resp = self._patch(
            authed_client, ws, {"provisioned_user_policies": ["sudo"]}
        )
        assert resp.status_code == 400

    def test_a_bare_string_is_rejected(self, authed_client, user, sensitive_grant):
        ws = services.create_workspace(user=user, name="Acme")
        resp = self._patch(
            authed_client, ws, {"provisioned_user_policies": "password_change"}
        )
        assert resp.status_code == 400

    def test_membership_is_untouched_by_a_rejected_patch(
        self, authed_client, user, sensitive_grant
    ):
        ws = services.create_workspace(user=user, name="Acme")
        self._patch(authed_client, ws, {"provisioned_user_policies": ["sudo"]})
        ws.refresh_from_db()
        assert ws.settings.get("security") is None
        assert WorkspaceMember.objects.filter(workspace=ws, user=user).exists()
