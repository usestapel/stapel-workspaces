"""require_mfa reports what it achieved, and enforces at the door (WORK-01).

The finding: ``PATCH settings.security.require_mfa=true`` saved the flag,
ran a sweep whose Boolean result the view discarded, and answered 200. The
sweep stopped at the first auth error, so an organization could be told
"MFA is required now" with none of its members checked — and nothing
retried, nothing recorded, and nothing stopped an unchecked member from
walking in afterwards.

Four things are pinned here:

1. **state, not silence** — the sweep writes a
   :class:`~stapel_workspaces.models.WorkspaceMFAEnforcement` record and the
   API answers with it, so ``enforced`` is distinguishable from "we tried";
2. **per-member compliance** — an answer nobody has got is NULL, not
   "fine": a member auth was never asked about is not the same row as a
   member auth confirmed;
3. **admission** — under the policy, an unverified member is asked at the
   door and refused while the answer is missing, however they got in
   (joined later, reinstated, missed by the sweep);
4. **durable retry** — an idempotent sweep any scheduler can run, which
   moves a workspace from failed/enforcing to enforced once auth answers.
"""

import pytest
from django.test import override_settings
from django.utils import timezone

from stapel_core.comm.exceptions import FunctionCallError
from stapel_core.comm.registry import function_registry

from stapel_workspaces import services
from stapel_workspaces.models import (
    MFAEnforcementState,
    Role,
    SUSPENSION_NO_MFA,
    WorkspaceMember,
    WorkspaceMFAEnforcement,
)
from stapel_workspaces.permissions import get_membership

BASE = "/workspaces/api/workspaces/v1"


@pytest.fixture
def mfa_status():
    """Scripted ``auth.mfa_status``: ``strong`` holds user pks that pass.

    ``fail`` makes the provider raise, standing in for auth being down —
    the case the old sweep treated as "carry on".
    """
    state = {"calls": [], "strong": set(), "fail": False}

    def provider(payload):
        state["calls"].append(payload)
        if state["fail"]:
            raise FunctionCallError("auth.mfa_status is down")
        return {"has_strong_mfa": payload["user_id"] in state["strong"]}

    function_registry.register(services.MFA_STATUS, provider)
    yield state
    function_registry._providers.pop(services.MFA_STATUS, None)
    function_registry._schemas.pop(services.MFA_STATUS, None)


def _mfa_workspace(owner, *, on=True):
    ws = services.create_workspace(user=owner, name="Acme")
    if on:
        ws.settings = {"security": {"require_mfa": True}}
        ws.save(update_fields=["settings"])
    return ws


def _member(ws, user, role=Role.MEMBER):
    return WorkspaceMember.objects.create(
        workspace=ws, user=user, role=role, accepted_at=timezone.now()
    )


@pytest.mark.django_db
class TestTheSweepRecordsWhatItAchieved:
    def test_full_coverage_is_enforced(self, user, other_user, mfa_status):
        ws = _mfa_workspace(user)
        _member(ws, other_user)
        mfa_status["strong"] = {str(user.pk), str(other_user.pk)}

        record = services.enforce_require_mfa(ws)

        assert record.state == MFAEnforcementState.ENFORCED
        assert record.checked_members == 2
        assert record.noncompliant_members == 0
        assert record.completed_at is not None
        assert record.last_error == ""

    def test_auth_failure_is_failed_not_enforced(
        self, user, other_user, mfa_status
    ):
        """The exact finding: the sweep could not check anyone, and said so.

        Before, this returned False into a caller that ignored it and the
        endpoint answered a clean 200.
        """
        ws = _mfa_workspace(user)
        _member(ws, other_user)
        mfa_status["fail"] = True

        record = services.enforce_require_mfa(ws)

        assert record.state == MFAEnforcementState.FAILED
        assert record.checked_members == 0
        assert record.last_error
        assert record.completed_at is None

    def test_a_failure_on_one_member_still_checks_the_rest(
        self, user, other_user, mfa_status, monkeypatch
    ):
        """One unreachable call is not a reason to stop asking about the org."""
        ws = _mfa_workspace(user)
        _member(ws, other_user)
        real_call = services.call
        seen = []

        def flaky(name, payload=None):
            seen.append(payload["user_id"])
            if len(seen) == 1:
                raise FunctionCallError("transient")
            return real_call(name, payload)

        mfa_status["strong"] = {str(user.pk), str(other_user.pk)}
        monkeypatch.setattr(services, "call", flaky)

        record = services.enforce_require_mfa(ws)

        assert len(seen) == 2, "the sweep stopped at the first error"
        assert record.state == MFAEnforcementState.FAILED
        assert record.checked_members == 1

    def test_the_noncompliant_are_suspended_and_recorded(
        self, user, other_user, mfa_status
    ):
        ws = _mfa_workspace(user)
        member = _member(ws, other_user)
        mfa_status["strong"] = {str(user.pk)}

        record = services.enforce_require_mfa(ws)

        member.refresh_from_db()
        assert member.mfa_compliant is False
        assert member.mfa_verified_at is not None
        assert member.suspension_reason == SUSPENSION_NO_MFA
        assert record.noncompliant_members == 1
        # Suspended members are not active, so coverage is complete.
        assert record.state == MFAEnforcementState.ENFORCED


@pytest.mark.django_db
class TestComplianceIsPerMember:
    def test_unasked_is_null_not_compliant(self, user, other_user, mfa_status):
        ws = _mfa_workspace(user)
        member = _member(ws, other_user)
        mfa_status["fail"] = True

        services.enforce_require_mfa(ws)

        member.refresh_from_db()
        assert member.mfa_compliant is None, (
            "an unreachable auth must not read as a passing member"
        )
        assert member.suspended_at is None, (
            "and must not suspend the org on a hiccup either"
        )

    def test_switching_the_policy_off_forgets_the_answers(
        self, user, other_user, mfa_status
    ):
        """A year-old "yes" must not admit anybody when MFA comes back on."""
        ws = _mfa_workspace(user)
        member = _member(ws, other_user)
        mfa_status["strong"] = {str(user.pk), str(other_user.pk)}
        services.enforce_require_mfa(ws)

        services.lift_no_mfa_suspensions(ws)

        member.refresh_from_db()
        assert member.mfa_compliant is None
        assert (
            WorkspaceMFAEnforcement.objects.get(workspace=ws).state
            == MFAEnforcementState.PENDING
        )


@pytest.mark.django_db
class TestAdmissionIsEnforcedEveryTime:
    def test_a_member_who_joined_after_the_sweep_is_checked(
        self, user, other_user, mfa_status
    ):
        """The gap the single sweep left: everybody who arrived later."""
        ws = _mfa_workspace(user)
        mfa_status["strong"] = {str(user.pk)}
        services.enforce_require_mfa(ws)

        latecomer = _member(ws, other_user)
        assert get_membership(ws.id, other_user.id) is None

        latecomer.refresh_from_db()
        assert latecomer.mfa_compliant is False
        assert latecomer.suspension_reason == SUSPENSION_NO_MFA

    def test_a_verified_member_is_admitted_without_asking_again(
        self, user, other_user, mfa_status
    ):
        ws = _mfa_workspace(user)
        _member(ws, other_user)
        mfa_status["strong"] = {str(user.pk), str(other_user.pk)}
        services.enforce_require_mfa(ws)
        calls_after_sweep = len(mfa_status["calls"])

        assert get_membership(ws.id, other_user.id) is not None
        assert len(mfa_status["calls"]) == calls_after_sweep

    def test_auth_unreachable_keeps_an_unverified_member_out(
        self, user, other_user, mfa_status
    ):
        """Fail-closed at the door — the containment the sweep cannot give.

        Suspending a whole organization on an auth hiccup is the wrong
        answer (an outage would lock everyone out permanently); refusing
        entry until somebody can be asked is reversible the moment auth is
        back.
        """
        ws = _mfa_workspace(user)
        _member(ws, other_user)
        mfa_status["fail"] = True

        assert get_membership(ws.id, other_user.id) is None
        assert (
            WorkspaceMember.objects.get(workspace=ws, user=other_user).suspended_at
            is None
        )

    def test_a_workspace_without_the_policy_is_untouched(
        self, user, other_user, mfa_status
    ):
        ws = _mfa_workspace(user, on=False)
        _member(ws, other_user)

        assert get_membership(ws.id, other_user.id) is not None
        assert mfa_status["calls"] == []


@pytest.mark.django_db
class TestTheRetryIsDurable:
    def test_retry_moves_a_failed_workspace_to_enforced(
        self, user, other_user, mfa_status
    ):
        ws = _mfa_workspace(user)
        _member(ws, other_user)
        mfa_status["fail"] = True
        assert services.enforce_require_mfa(ws).state == MFAEnforcementState.FAILED

        mfa_status["fail"] = False
        mfa_status["strong"] = {str(user.pk), str(other_user.pk)}
        (record,) = services.retry_mfa_enforcement()

        assert record.workspace_id == ws.id
        assert record.state == MFAEnforcementState.ENFORCED
        assert record.attempts == 2

    def test_retry_leaves_enforced_workspaces_alone(
        self, user, other_user, mfa_status
    ):
        ws = _mfa_workspace(user)
        _member(ws, other_user)
        mfa_status["strong"] = {str(user.pk), str(other_user.pk)}
        services.enforce_require_mfa(ws)

        assert services.retry_mfa_enforcement() == []

    def test_retry_is_idempotent(self, user, other_user, mfa_status):
        ws = _mfa_workspace(user)
        _member(ws, other_user)
        mfa_status["fail"] = True
        services.enforce_require_mfa(ws)

        first = services.retry_mfa_enforcement()
        second = services.retry_mfa_enforcement()

        assert len(first) == len(second) == 1
        assert first[0].state == second[0].state == MFAEnforcementState.FAILED
        assert WorkspaceMFAEnforcement.objects.count() == 1

    def test_the_command_runs_the_sweep(self, user, other_user, mfa_status):
        from io import StringIO

        from django.core.management import call_command

        ws = _mfa_workspace(user)
        _member(ws, other_user)
        mfa_status["fail"] = True
        services.enforce_require_mfa(ws)

        mfa_status["fail"] = False
        mfa_status["strong"] = {str(user.pk), str(other_user.pk)}
        out = StringIO()
        call_command("enforce_workspace_mfa", stdout=out)

        assert "swept 1 workspace(s); 0 still incomplete" in out.getvalue()
        assert (
            WorkspaceMFAEnforcement.objects.get(workspace=ws).state
            == MFAEnforcementState.ENFORCED
        )


@pytest.mark.django_db
class TestTheApiSaysWhatHolds:
    def _patch_security(self, client, ws, block):
        return client.patch(
            f"{BASE}/{ws.id}", {"settings": {"security": block}}, format="json"
        )

    def test_turning_it_on_with_auth_down_does_not_claim_enforced(
        self, authed_client, user, other_user, mfa_status
    ):
        """The headline of WORK-01: a 200 that used to mean nothing."""
        from stapel_core.verification import grant_verification

        ws = services.create_workspace(user=user, name="Acme")
        _member(ws, other_user)
        mfa_status["fail"] = True
        grant_verification(user_id=str(user.pk), scope="sensitive", max_age=300)

        resp = self._patch_security(authed_client, ws, {"require_mfa": True})

        assert resp.status_code == 200, resp.content
        body = resp.json()
        assert body["settings"]["security"]["require_mfa"] is True
        enforcement = body["mfa_enforcement"]
        assert enforcement["state"] == MFAEnforcementState.FAILED
        assert enforcement["unverified_members"] == 2
        assert enforcement["last_error"]

    def test_the_roster_shows_who_is_noncompliant(
        self, authed_client, user, other_user, mfa_status
    ):
        from stapel_core.verification import grant_verification

        ws = services.create_workspace(user=user, name="Acme")
        _member(ws, other_user)
        mfa_status["strong"] = {str(user.pk)}
        grant_verification(user_id=str(user.pk), scope="sensitive", max_age=300)
        self._patch_security(authed_client, ws, {"require_mfa": True})

        resp = authed_client.get(f"{BASE}/{ws.id}/members")

        assert resp.status_code == 200, resp.content
        by_user = {m["user_id"]: m for m in resp.json()["items"]}
        assert by_user[str(user.pk)]["mfa_compliant"] is True
        assert by_user[str(other_user.pk)]["mfa_compliant"] is False


@pytest.mark.django_db
class TestTheCrossServiceAnswersAgree:
    """The door is one door: comm and the internal endpoint use it too.

    Another service asking "is this person a member" is asking an admission
    question, and a member whose second factor the workspace requires and
    nobody has confirmed must not be answered "yes, with these
    capabilities" there while the HTTP surface refuses them.
    """

    def test_check_membership_refuses_an_unverified_member(
        self, user, other_user, mfa_status
    ):
        from stapel_workspaces.functions import check_membership

        ws = _mfa_workspace(user)
        _member(ws, other_user)
        mfa_status["fail"] = True

        answer = check_membership(
            {"workspace_id": str(ws.id), "user_id": str(other_user.pk)}
        )

        assert answer == {"is_member": False, "role": None, "capabilities": []}

    @override_settings(
        MIDDLEWARE=["stapel_core.django.jwt.middleware.ServiceAPIKeyMiddleware"],
        SERVICE_API_KEY="test-service-key",
    )
    def test_the_internal_endpoint_refuses_an_unverified_member(
        self, api_client, user, other_user, mfa_status
    ):
        ws = _mfa_workspace(user)
        _member(ws, other_user)
        mfa_status["fail"] = True

        resp = api_client.get(
            f"{BASE}/internal/{ws.id}/members/{other_user.pk}",
            HTTP_X_API_KEY="test-service-key",
        )

        assert resp.status_code == 404
