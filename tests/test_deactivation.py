"""Account deactivation reaches the workspaces (#92).

Before this, ``is_active=False`` in auth stopped exactly one thing — the
login — and a deactivated user kept every membership, kept appearing in
every member list and kept costing the owner a seat.

What these tests pin:

* ``user.deactivated`` suspends every membership the account holds, through
  the EXISTING suspension mechanism (``suspended_at`` + reason), with no
  row destroyed;
* redelivery is a no-op that does not move the original ``suspended_at``;
* ``user.reactivated`` puts them back — and lifts only what deactivation
  set, so an MFA-less user does not sneak back into a require_mfa org;
* the GDPR erasure event ``user.deleted`` still goes its own, row-deleting
  way and is not replaced by any of this;
* a suspended member does not hold a billable seat.
"""

import json
import types
from pathlib import Path

import jsonschema
import pytest
from django.utils import timezone

import stapel_workspaces
from stapel_workspaces import entitlements, services
from stapel_workspaces.actions import (
    handle_user_deactivated,
    handle_user_deleted,
    handle_user_reactivated,
)
from stapel_workspaces.models import (
    SUSPENSION_ACCOUNT_DEACTIVATED,
    SUSPENSION_NO_MFA,
    MemberState,
    Role,
    Workspace,
    WorkspaceMember,
)

CONSUMES_DIR = (
    Path(stapel_workspaces.__file__).resolve().parent / "schemas" / "consumes"
)


def _event(user_id, **payload):
    return types.SimpleNamespace(
        payload={"user_id": str(user_id), **payload}, event_id="evt-1"
    )


def _validate_consumed(payload, event_name):
    jsonschema.validate(
        payload,
        json.loads((CONSUMES_DIR / f"{event_name}.json").read_text()),
        format_checker=jsonschema.FormatChecker(),
    )


def _ws(user, name="Acme"):
    return services.create_workspace(user=user, name=name)


def _member(ws, user, role=Role.MEMBER, **kwargs):
    return WorkspaceMember.objects.create(
        workspace=ws, user=user, role=role, accepted_at=timezone.now(), **kwargs
    )


@pytest.mark.django_db
class TestDeactivationSuspendsMemberships:
    def test_every_membership_is_suspended(self, user, other_user):
        one = _ws(user, "One")
        two = _ws(user, "Two")
        m1 = _member(one, other_user)
        m2 = _member(two, other_user)

        handle_user_deactivated(_event(other_user.pk, reason="abuse"))

        for m in (m1, m2):
            m.refresh_from_db()
            assert m.suspended_at is not None
            assert m.suspension_reason == SUSPENSION_ACCOUNT_DEACTIVATED
            assert m.state == MemberState.SUSPENDED

    def test_state_is_suspended_not_deleted(self, user, other_user):
        """The row, the role and the history survive — that is the whole
        difference between ``suspended`` and ``deleted``."""
        ws = _ws(user)
        member = _member(ws, other_user, role=Role.ADMIN)

        handle_user_deactivated(_event(other_user.pk))

        member.refresh_from_db()
        assert member.state == MemberState.SUSPENDED
        assert member.role == Role.ADMIN
        assert WorkspaceMember.objects.filter(pk=member.pk).exists()

    def test_membership_stops_counting_for_access(self, user, other_user):
        from stapel_workspaces.permissions import get_membership, has_capability

        ws = _ws(user)
        _member(ws, other_user)
        assert has_capability(ws.id, other_user.id, "workspace.view")

        handle_user_deactivated(_event(other_user.pk))

        assert get_membership(ws.id, other_user.id) is None
        assert not has_capability(ws.id, other_user.id, "workspace.view")

    def test_suspension_is_policy_independent(self, user, other_user):
        """Unlike the MFA consumer, deactivation does not ask the workspace
        for permission: no security setting makes a deactivated account
        admissible."""
        ws = _ws(user)
        member = _member(ws, other_user)
        assert not services.security_settings_for(ws).require_mfa

        assert handle_user_deactivated(_event(other_user.pk)) is None
        member.refresh_from_db()
        assert member.suspended_at is not None

    def test_emits_member_suspended_per_workspace(self, user, other_user):
        from stapel_core.comm import subscribe_action

        events = []
        subscribe_action("workspace.member_suspended", events.append)
        ws = _ws(user)
        _member(ws, other_user)

        handle_user_deactivated(_event(other_user.pk))

        assert [e.payload["reason"] for e in events] == [
            SUSPENSION_ACCOUNT_DEACTIVATED
        ]

    def test_pending_invitation_row_is_untouched(self, user, other_user):
        """Not accepted yet ⇒ not a membership to suspend."""
        ws = _ws(user)
        member = WorkspaceMember.objects.create(
            workspace=ws, user=other_user, role=Role.MEMBER
        )
        handle_user_deactivated(_event(other_user.pk))
        member.refresh_from_db()
        assert member.state == MemberState.INVITED
        assert member.suspended_at is None

    def test_soft_deleted_workspace_is_skipped(self, user, other_user):
        ws = _ws(user)
        member = _member(ws, other_user)
        Workspace.objects.filter(pk=ws.pk).update(deleted_at=timezone.now())

        handle_user_deactivated(_event(other_user.pk))

        member.refresh_from_db()
        assert member.suspended_at is None

    def test_missing_user_id_is_logged_not_raised(self):
        handle_user_deactivated(types.SimpleNamespace(payload={}, event_id="e"))
        handle_user_reactivated(types.SimpleNamespace(payload={}, event_id="e"))

    def test_payloads_match_the_declared_consumes_schemas(self, user):
        _validate_consumed(
            {"user_id": str(user.pk), "reason": "abuse", "actor_id": str(user.pk)},
            "user.deactivated",
        )
        _validate_consumed({"user_id": str(user.pk)}, "user.deactivated")
        _validate_consumed({"user_id": str(user.pk)}, "user.reactivated")


@pytest.mark.django_db
class TestIdempotence:
    def test_redelivery_does_not_move_the_first_suspension(self, user, other_user):
        ws = _ws(user)
        member = _member(ws, other_user)

        handle_user_deactivated(_event(other_user.pk))
        member.refresh_from_db()
        first = member.suspended_at

        handle_user_deactivated(_event(other_user.pk))
        member.refresh_from_db()
        assert member.suspended_at == first
        assert member.suspension_reason == SUSPENSION_ACCOUNT_DEACTIVATED

    def test_redelivery_emits_nothing_the_second_time(self, user, other_user):
        from stapel_core.comm import subscribe_action

        events = []
        subscribe_action("workspace.member_suspended", events.append)
        ws = _ws(user)
        _member(ws, other_user)

        handle_user_deactivated(_event(other_user.pk))
        handle_user_deactivated(_event(other_user.pk))
        assert len(events) == 1

    def test_reactivation_redelivery_is_a_no_op(self, user, other_user):
        ws = _ws(user)
        member = _member(ws, other_user)
        handle_user_deactivated(_event(other_user.pk))

        handle_user_reactivated(_event(other_user.pk))
        handle_user_reactivated(_event(other_user.pk))

        member.refresh_from_db()
        assert member.state == MemberState.ACTIVE

    def test_deactivating_an_already_no_mfa_suspended_member_keeps_the_reason(
        self, user, other_user
    ):
        ws = _ws(user)
        member = _member(ws, other_user)
        services.suspend_member(member, reason=SUSPENSION_NO_MFA, notify=False)
        member.refresh_from_db()
        first = member.suspended_at

        handle_user_deactivated(_event(other_user.pk))

        member.refresh_from_db()
        assert member.suspension_reason == SUSPENSION_NO_MFA
        assert member.suspended_at == first


@pytest.mark.django_db
class TestReactivation:
    def test_memberships_come_back(self, user, other_user):
        from stapel_workspaces.permissions import has_capability

        ws = _ws(user)
        member = _member(ws, other_user, role=Role.ADMIN)

        handle_user_deactivated(_event(other_user.pk))
        handle_user_reactivated(_event(other_user.pk))

        member.refresh_from_db()
        assert member.state == MemberState.ACTIVE
        assert member.suspension_reason == ""
        assert member.role == Role.ADMIN
        assert has_capability(ws.id, other_user.id, "workspace.view")

    def test_only_account_deactivated_suspensions_lift(self, user, other_user):
        """A restored account must not walk back into a require_mfa
        workspace it was locked out of for having no second factor."""
        ws_mfa = _ws(user, "Strict")
        ws_plain = _ws(user, "Plain")
        m_mfa = _member(ws_mfa, other_user)
        m_plain = _member(ws_plain, other_user)
        services.suspend_member(m_mfa, reason=SUSPENSION_NO_MFA, notify=False)

        handle_user_deactivated(_event(other_user.pk))
        handle_user_reactivated(_event(other_user.pk))

        m_mfa.refresh_from_db()
        m_plain.refresh_from_db()
        assert m_mfa.state == MemberState.SUSPENDED
        assert m_mfa.suspension_reason == SUSPENSION_NO_MFA
        assert m_plain.state == MemberState.ACTIVE

    def test_reactivating_a_never_deactivated_user_changes_nothing(
        self, user, other_user
    ):
        ws = _ws(user)
        member = _member(ws, other_user)
        handle_user_reactivated(_event(other_user.pk))
        member.refresh_from_db()
        assert member.state == MemberState.ACTIVE


@pytest.mark.django_db
class TestGdprDeletionKeepsItsOwnPath:
    def test_user_deleted_still_removes_rows(self, user, other_user):
        """The GDPR event is irreversible and row-destroying — deactivation
        must not have taken it over."""
        ws = _ws(user)
        _member(ws, other_user)

        handle_user_deleted(_event(other_user.pk))

        assert not WorkspaceMember.objects.filter(user=other_user).exists()

    def test_deactivation_never_deletes(self, user, other_user):
        ws = _ws(user)
        _member(ws, other_user)

        handle_user_deactivated(_event(other_user.pk))

        assert WorkspaceMember.objects.filter(user=other_user).count() == 1

    def test_owned_workspaces_survive_deactivation(self, user):
        """GDPR deletion soft-deletes the accounts's owned workspaces;
        deactivation must leave them alone — the org keeps running while its
        owner's login is off."""
        ws = _ws(user)

        handle_user_deactivated(_event(user.pk))

        ws.refresh_from_db()
        assert ws.deleted_at is None


@pytest.mark.django_db
class TestSuspendedMembersDoNotHoldSeats:
    def test_suspended_member_is_not_billed(self, user, other_user):
        ws = _ws(user)
        member = _member(ws, other_user)
        # owner + member
        assert entitlements.member_seats_quantity(ws) == 2

        services.suspend_member(member, reason=SUSPENSION_NO_MFA, notify=False)
        assert entitlements.member_seats_quantity(ws) == 1

    def test_deactivation_frees_the_seat_and_reactivation_takes_it_back(
        self, user, other_user
    ):
        ws = _ws(user)
        _member(ws, other_user)
        assert entitlements.member_seats_quantity(ws) == 2

        handle_user_deactivated(_event(other_user.pk))
        assert entitlements.member_seats_quantity(ws) == 1

        handle_user_reactivated(_event(other_user.pk))
        assert entitlements.member_seats_quantity(ws) == 2

    def test_pending_invitations_still_reserve_seats(self, user):
        ws = _ws(user)
        services.create_invitation(
            workspace=ws, email="live@example.com", role="member", invited_by=user
        )
        assert entitlements.member_seats_quantity(ws) == 2
        assert entitlements.member_seats_quantity(ws, additional=3) == 5
