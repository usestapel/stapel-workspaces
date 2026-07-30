"""An org admin resets a member's password — and none of the five ways it
goes wrong (#110).

A password reset performed by somebody other than the account holder is an
account takeover done on purpose. The endpoint is three lines of plumbing
and five security questions, and this file is one section per question.

1. **Who may do it** — a mandate, not a session. Capability
   ``members.password.reset``, declared ``high``, so a fresh step-up
   (``sensitive``) is demanded on top of being logged in. Only an owner may
   reset an OWNER's password, or an admin resets the owner and inherits the
   organization. Auth refuses a staff/superuser target outright: org admin
   is a role inside one workspace, deployment staff is a role above every
   workspace, and the first must never be a route to the second.
2. **Does the user find out** — always, by letter naming the workspace and
   the admin. A reset is indistinguishable from a takeover unless the
   holder is told which one it was. The letter never carries the new
   password.
3. **Is the new password temporary** — yes: auth raises the workspace's
   ``provisioned_user_policies`` (#90), defaulting to ``password_change``,
   and since auth 0.15.0 that demand holds on every session-issuance path.
4. **Is it an existence oracle** — no. :class:`TestNotAnExistenceOracle`
   compares the responses for four different kinds of unusable target
   **byte for byte**.
5. **Is it logged with the actor** — twice: the org's transactional-outbox
   event and auth's own audit row, neither carrying credential material.

Auth's half (session revocation, the audit row, the privileged-account
refusal, the password canon) is scripted here — this suite owns the
workspaces half of the seam. ``stapel-auth``'s
``tests/test_admin_password_reset.py`` owns the other half.
"""

import uuid

import pytest
from django.utils import timezone
from stapel_core.comm import subscribe_action
from stapel_core.comm.registry import function_registry
from stapel_core.verification import grant_verification

from stapel_workspaces import services
from stapel_workspaces.capabilities import capability_level, role_has_capability
from stapel_workspaces.errors import (
    ERR_403_FORBIDDEN_WORKSPACE,
    ERR_403_MISSING_CAPABILITY,
    ERR_404_MEMBER_NOT_FOUND,
    ERR_503_AUTH_UNAVAILABLE,
)
from stapel_workspaces.models import Role, WorkspaceMember

BASE = "/workspaces/api/workspaces/v1"
GENERATED = "server-generated-secret-xyz"


class Recorder(list):
    """Payloads a scripted comm provider was sent, plus a scripted reply."""

    result = None


@pytest.fixture
def fake_auth_reset():
    """Scripted ``auth.admin_reset_password``; records the payloads."""
    calls = Recorder()

    def provider(payload):
        calls.append(payload)
        if calls.result is not None:
            return calls.result
        return {
            "generated_password": GENERATED,
            "sessions_revoked": 2,
            "first_login_policies_applied": list(
                payload.get("first_login_policies") or []
            ),
        }

    function_registry._providers.pop(services.ADMIN_RESET_PASSWORD, None)
    function_registry._schemas.pop(services.ADMIN_RESET_PASSWORD, None)
    function_registry.register(services.ADMIN_RESET_PASSWORD, provider)
    yield calls
    function_registry._providers.pop(services.ADMIN_RESET_PASSWORD, None)
    function_registry._schemas.pop(services.ADMIN_RESET_PASSWORD, None)


@pytest.fixture
def no_auth_reset_seam():
    """The deployment runs an older auth: the Function is not registered."""
    function_registry._providers.pop(services.ADMIN_RESET_PASSWORD, None)
    function_registry._schemas.pop(services.ADMIN_RESET_PASSWORD, None)
    yield


@pytest.fixture
def resets():
    """Collect ``workspace.member_password_reset`` events off the comm bus."""
    events = []
    subscribe_action("workspace.member_password_reset", events.append)
    return events


@pytest.fixture
def sent_notifications(monkeypatch):
    """Capture ``request_notification`` calls made by the service layer."""
    sent = []

    def fake(notification_type=None, **kwargs):
        sent.append((notification_type, kwargs))
        return True

    import stapel_core.notifications as notifications

    monkeypatch.setattr(notifications, "request_notification", fake)
    return sent


def _new_user(suffix=""):
    from stapel_core.django.users.models import User

    return User.objects.create_user(
        username=f"u-{suffix}{uuid.uuid4().hex[:8]}",
        email=f"{uuid.uuid4().hex[:8]}@example.com",
        password="testpass-1234",
    )


@pytest.fixture
def org(db, user):
    class Org:
        pass

    o = Org()
    o.admin = user  # workspace owner, holds every capability
    o.ws = services.create_workspace(user=o.admin, name="Acme")
    o.member = _new_user("member-")
    WorkspaceMember.objects.create(
        workspace=o.ws,
        user=o.member,
        role=Role.MEMBER,
        accepted_at=timezone.now(),
    )
    return o


@pytest.fixture
def admin_client(api_client, org):
    api_client.force_authenticate(user=org.admin)
    grant_verification(user_id=str(org.admin.pk), scope="sensitive", max_age=300)
    return api_client


def _url(ws, target_id):
    return f"{BASE}/{ws.id}/members/{target_id}/password/reset"


def _reset(client, ws, target_id, **body):
    return client.post(_url(ws, target_id), body, format="json")


# ---------------------------------------------------------------------------
# 1. Who may do it — a mandate, not a session
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestTheMandate:
    def test_the_happy_path(self, admin_client, org, fake_auth_reset):
        resp = _reset(admin_client, org.ws, org.member.id)
        assert resp.status_code == 200, resp.content
        body = resp.json()
        assert body["user_id"] == str(org.member.id)
        assert body["generated_password"] == GENERATED
        assert body["sessions_revoked"] == 2
        assert fake_auth_reset[0]["user_id"] == str(org.member.id)

    def test_the_capability_exists_and_is_high(self):
        """Declared, not implied: a `high` capability is what makes the
        step-up decorator bite. Same level as minting an account outright."""
        assert role_has_capability(Role.ADMIN, "members.password.reset")
        assert role_has_capability(Role.OWNER, "members.password.reset")
        assert not role_has_capability(Role.MEMBER, "members.password.reset")
        assert not role_has_capability(Role.VIEWER, "members.password.reset")
        assert capability_level("members.password.reset") == "high"

    def test_an_ordinary_member_is_refused(
        self, api_client, org, db, fake_auth_reset
    ):
        """Being logged in and being IN the workspace is not the mandate."""
        plain = _new_user("plain-")
        WorkspaceMember.objects.create(
            workspace=org.ws, user=plain, role=Role.MEMBER, accepted_at=timezone.now()
        )
        api_client.force_authenticate(user=plain)
        grant_verification(user_id=str(plain.pk), scope="sensitive", max_age=300)
        resp = _reset(api_client, org.ws, org.member.id)
        assert resp.status_code == 403
        assert resp.json()["localizable_error"] == ERR_403_MISSING_CAPABILITY
        assert fake_auth_reset == []

    def test_step_up_is_demanded_on_top_of_the_capability(
        self, api_client, org, fake_auth_reset
    ):
        """An ambient session must not be enough to hand over an account.

        Same authenticated owner as the happy path — only the fresh
        `sensitive` grant is missing.
        """
        api_client.force_authenticate(user=org.admin)
        resp = _reset(api_client, org.ws, org.member.id)
        assert resp.status_code == 403
        body = resp.json()
        assert body["verification"]["scope"] == "sensitive"
        assert fake_auth_reset == []

    def test_only_an_owner_may_reset_an_owner(
        self, api_client, org, db, fake_auth_reset
    ):
        """Otherwise an admin resets the owner and inherits the org."""
        admin = _new_user("admin-")
        WorkspaceMember.objects.create(
            workspace=org.ws, user=admin, role=Role.ADMIN, accepted_at=timezone.now()
        )
        api_client.force_authenticate(user=admin)
        grant_verification(user_id=str(admin.pk), scope="sensitive", max_age=300)
        resp = _reset(api_client, org.ws, org.admin.id)
        assert resp.status_code == 403
        assert resp.json()["localizable_error"] == ERR_403_FORBIDDEN_WORKSPACE
        assert fake_auth_reset == []

    def test_a_privileged_target_is_refused_by_auth(
        self, admin_client, org, fake_auth_reset
    ):
        """Org admin is a role inside one workspace; staff is above them all.

        The boundary is auth's to hold — this module does not know who is
        staff — and its refusal passes through keyed.
        """
        fake_auth_reset.result = {"error": "error.403.privileged_account"}
        resp = _reset(admin_client, org.ws, org.member.id)
        assert resp.status_code == 403
        assert resp.json()["localizable_error"] == "error.403.privileged_account"

    def test_anonymous_is_refused(self, api_client, org):
        assert _reset(api_client, org.ws, org.member.id).status_code in (401, 403)


# ---------------------------------------------------------------------------
# 2. Does the user find out
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestTheMemberIsTold:
    def test_a_letter_names_the_workspace_and_the_actor(
        self, admin_client, org, fake_auth_reset, sent_notifications
    ):
        """A reset is indistinguishable from a takeover unless the account
        holder is told which one it was — and by whom."""
        resp = _reset(admin_client, org.ws, org.member.id)
        assert resp.status_code == 200, resp.content
        assert len(sent_notifications) == 1
        kind, kwargs = sent_notifications[0]
        assert kind == "workspace.member_password_reset"
        assert kwargs["user_id"] == str(org.member.id)
        assert kwargs["variables"]["workspace_name"] == org.ws.name
        assert kwargs["variables"]["actor_name"]
        assert resp.json()["notified"] is True

    def test_the_letter_never_carries_the_new_password(
        self, admin_client, org, fake_auth_reset, sent_notifications
    ):
        """The admin who ordered the reset holds the credential and hands
        it over out of band. A security alert that also contains the
        credential is worth nothing as an alert."""
        _reset(admin_client, org.ws, org.member.id)
        assert GENERATED not in str(sent_notifications)

    def test_a_delivery_failure_does_not_undo_the_reset(
        self, admin_client, org, fake_auth_reset, monkeypatch
    ):
        """The credential already changed in auth; there is nothing to roll
        back, and the response says so instead of pretending."""
        import stapel_core.notifications as notifications

        def boom(*a, **kw):
            raise RuntimeError("notifications down")

        monkeypatch.setattr(notifications, "request_notification", boom)
        resp = _reset(admin_client, org.ws, org.member.id)
        assert resp.status_code == 200, resp.content
        assert resp.json()["notified"] is False


# ---------------------------------------------------------------------------
# 3. Is the new password temporary
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestTheNewPasswordIsTemporary:
    def test_the_workspace_policies_are_demanded_by_default(
        self, admin_client, org, fake_auth_reset
    ):
        """#90's machinery, reused: a password the admin knows must stop
        working at its first use."""
        resp = _reset(admin_client, org.ws, org.member.id)
        assert fake_auth_reset[0]["first_login_policies"] == ["password_change"]
        assert resp.json()["first_login_policies_applied"] == ["password_change"]

    def test_the_workspace_can_demand_both_steps(
        self, admin_client, org, fake_auth_reset
    ):
        org.ws.settings = {
            "security": {
                "provisioned_user_policies": ["password_change", "mfa_enroll"]
            }
        }
        org.ws.save(update_fields=["settings"])
        _reset(admin_client, org.ws, org.member.id)
        assert fake_auth_reset[0]["first_login_policies"] == [
            "password_change",
            "mfa_enroll",
        ]

    def test_the_request_may_override_the_workspace_default(
        self, admin_client, org, fake_auth_reset
    ):
        _reset(
            admin_client,
            org.ws,
            org.member.id,
            first_login_policies=["mfa_enroll"],
        )
        assert fake_auth_reset[0]["first_login_policies"] == ["mfa_enroll"]

    def test_suppressing_the_demand_is_explicit(
        self, admin_client, org, fake_auth_reset
    ):
        """An empty list is a decision that reaches auth's audit row; an
        omitted field is not the same thing and means "the org's own"."""
        _reset(admin_client, org.ws, org.member.id, first_login_policies=[])
        assert fake_auth_reset[0]["first_login_policies"] == []

    def test_an_unknown_policy_is_rejected_before_auth_is_called(
        self, admin_client, org, fake_auth_reset
    ):
        resp = _reset(
            admin_client, org.ws, org.member.id, first_login_policies=["sudo"]
        )
        assert resp.status_code == 400
        assert fake_auth_reset == []

    def test_an_admin_chosen_password_is_forwarded_and_not_echoed(
        self, admin_client, org, fake_auth_reset
    ):
        fake_auth_reset.result = {
            "sessions_revoked": 0,
            "first_login_policies_applied": ["password_change"],
        }
        resp = _reset(
            admin_client, org.ws, org.member.id, password="chosen-by-the-admin-9"
        )
        assert fake_auth_reset[0]["password"] == "chosen-by-the-admin-9"
        assert resp.json()["generated_password"] is None


# ---------------------------------------------------------------------------
# 4. Is it an existence oracle
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestNotAnExistenceOracle:
    """Four kinds of unusable target, one byte-identical answer.

    The pattern the invitation endpoints already follow: an admin controls
    both halves of the URL, so anything that distinguishes "no such
    account" from "an account that is not in your workspace" turns a
    workspace-scoped endpoint into a directory of the whole deployment.
    """

    def _bodies(self, client, org):
        stranger = _new_user("stranger-")
        other_ws = services.create_workspace(user=stranger, name="Other")
        their_member = _new_user("theirs-")
        WorkspaceMember.objects.create(
            workspace=other_ws,
            user=their_member,
            role=Role.MEMBER,
            accepted_at=timezone.now(),
        )
        return {
            "unknown uuid": _reset(client, org.ws, uuid.uuid4()),
            "real account, not a member": _reset(client, org.ws, stranger.id),
            "member of another workspace": _reset(client, org.ws, their_member.id),
            "the caller themselves": _reset(client, org.ws, org.admin.id),
        }

    def test_all_four_answer_identically(self, admin_client, org, fake_auth_reset):
        responses = self._bodies(admin_client, org)
        shapes = {
            label: (resp.status_code, resp.content)
            for label, resp in responses.items()
        }
        distinct = set(shapes.values())
        assert len(distinct) == 1, f"the endpoint distinguishes targets: {shapes}"
        status, content = distinct.pop()
        assert status == 404
        assert b"member_not_found" in content

    def test_the_shared_answer_is_the_member_not_found_key(
        self, admin_client, org, fake_auth_reset
    ):
        resp = _reset(admin_client, org.ws, uuid.uuid4())
        assert resp.json()["localizable_error"] == ERR_404_MEMBER_NOT_FOUND

    def test_no_unusable_target_reaches_auth(
        self, admin_client, org, fake_auth_reset
    ):
        """Not even as a call that auth then refuses: a timing or logging
        difference is an oracle too."""
        self._bodies(admin_client, org)
        assert fake_auth_reset == []

    def test_a_caller_without_the_mandate_learns_nothing_either(
        self, api_client, org, db, fake_auth_reset
    ):
        """The capability is checked BEFORE any target row is read, so the
        403 is the same whether or not the target exists."""
        plain = _new_user("plain2-")
        WorkspaceMember.objects.create(
            workspace=org.ws, user=plain, role=Role.MEMBER, accepted_at=timezone.now()
        )
        api_client.force_authenticate(user=plain)
        grant_verification(user_id=str(plain.pk), scope="sensitive", max_age=300)
        real = _reset(api_client, org.ws, org.member.id)
        fake = _reset(api_client, org.ws, uuid.uuid4())
        assert real.status_code == fake.status_code == 403
        assert real.content == fake.content

    def test_your_own_password_is_not_this_endpoint_s_business(
        self, admin_client, org, fake_auth_reset
    ):
        """Self-service password change lives in auth. Folding it in here
        would hand a step-up holder a way to change their own password
        without knowing the old one."""
        resp = _reset(admin_client, org.ws, org.admin.id)
        assert resp.status_code == 404
        assert fake_auth_reset == []


# ---------------------------------------------------------------------------
# 5. Is it logged, with the actor
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestTheActionIsOnTheRecord:
    def test_the_org_event_names_the_actor(
        self, admin_client, org, fake_auth_reset, resets
    ):
        _reset(admin_client, org.ws, org.member.id)
        assert len(resets) == 1
        assert resets[0].payload == {
            "workspace_id": str(org.ws.id),
            "user_id": str(org.member.id),
            "role": Role.MEMBER,
            "reset_by": str(org.admin.id),
            "sessions_revoked": 2,
        }

    def test_the_event_carries_no_credential_material(
        self, admin_client, org, fake_auth_reset, resets
    ):
        """It fans out to every subscriber; the password goes to exactly
        one place, once."""
        _reset(admin_client, org.ws, org.member.id)
        assert GENERATED not in str(resets[0].payload)

    def test_the_actor_reaches_auths_own_journal(
        self, admin_client, org, fake_auth_reset
    ):
        """Two records on purpose: the org's activity log and the
        deployment's security journal, and they must agree on who."""
        _reset(admin_client, org.ws, org.member.id, reason="ticket SUP-42")
        assert fake_auth_reset[0]["actor_id"] == str(org.admin.id)
        assert fake_auth_reset[0]["reason"] == "ticket SUP-42"

    def test_a_refused_reset_emits_nothing(
        self, admin_client, org, fake_auth_reset, resets
    ):
        fake_auth_reset.result = {"error": "error.403.privileged_account"}
        _reset(admin_client, org.ws, org.member.id)
        assert resets == []


# ---------------------------------------------------------------------------
# The seam itself
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestTheAuthSeamNeverDegradesToSuccess:
    def test_auth_unavailable_is_an_honest_503(
        self, admin_client, org, no_auth_reset_seam, resets, sent_notifications
    ):
        """Nothing happened, and the answer says nothing happened — a
        reported-success reset is worse than a failed one, because the
        admin then tells the member a password that does not work."""
        resp = _reset(admin_client, org.ws, org.member.id)
        assert resp.status_code == 503
        assert resp.json()["localizable_error"] == ERR_503_AUTH_UNAVAILABLE
        assert resets == []
        assert sent_notifications == []
