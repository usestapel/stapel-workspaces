"""WHO LET THIS PERSON IN, AND WHEN.

Nothing in this module could answer that. The comm events it emits are
fire-and-forget notifications to other services — nothing keeps them — and half
the transitions the owner listed emit nothing at all: an invitation created, an
invitation accepted, an account born from one. So "the admin section has no
audit of workspace members" was literally true: there was no record to read.

These tests pin three things: that each transition writes its line, that the
line is readable only by someone who may see the roster at all, and — the gate
that matters for the future — that a membership event this module EMITS cannot
ship without a matching audit action.
"""
import pytest
from django.test import override_settings

from stapel_workspaces import events as ws_events
from stapel_workspaces.models import (
    AuditAction,
    Role,
    WorkspaceAuditEvent,
    WorkspaceMember,
)
from stapel_workspaces.services import (
    create_invitation,
    create_workspace,
    decline_invitation,
    revoke_invitation,
    suspend_member,
    unsuspend_member,
)

BASE = "/workspaces/api/workspaces/v1"


def _actions(workspace):
    return list(
        WorkspaceAuditEvent.objects.filter(workspace=workspace)
        .order_by("created_at")
        .values_list("action", flat=True)
    )


@pytest.mark.django_db
class TestTransitionsAreRecorded:
    def test_an_invitation_writes_a_line_naming_who_sent_it(self, user):
        ws = create_workspace(user=user, name="Acme")
        create_invitation(
            workspace=ws, email="New@Acme.test", role=Role.MEMBER, invited_by=user
        )
        row = WorkspaceAuditEvent.objects.get(action=AuditAction.INVITATION_CREATED)
        assert row.actor_id == user.pk
        # Normalised, like the invitation itself — an audit that stores a
        # different spelling of the address than the invite did cannot be
        # joined against it.
        assert row.subject_email == "new@acme.test"
        assert row.role == Role.MEMBER

    def test_acceptance_writes_TWO_lines_and_they_are_different_facts(
        self, user, other_user
    ):
        """"This invitation was taken up" and "this person is now in the
        organization" can come apart — a re-accept joins nobody."""
        ws = create_workspace(user=user, name="Acme")
        inv = create_invitation(
            workspace=ws, email=other_user.email, role=Role.MEMBER, invited_by=user
        )
        from stapel_workspaces.services import accept_invitation

        accept_invitation(invitation=inv, user=other_user)
        assert _actions(ws) == [
            AuditAction.INVITATION_CREATED,
            AuditAction.INVITATION_ACCEPTED,
            AuditAction.MEMBER_JOINED,
        ]

    def test_a_revocation_names_the_admin_who_withdrew_it(self, user):
        ws = create_workspace(user=user, name="Acme")
        inv = create_invitation(
            workspace=ws, email="gone@acme.test", role=Role.MEMBER, invited_by=user
        )
        revoke_invitation(invitation=inv, revoked_by=user)
        row = WorkspaceAuditEvent.objects.get(action=AuditAction.INVITATION_REVOKED)
        assert row.actor_id == user.pk
        assert row.subject_email == "gone@acme.test"

    def test_a_decline_names_the_invitee(self, user, other_user):
        ws = create_workspace(user=user, name="Acme")
        inv = create_invitation(
            workspace=ws, email=other_user.email, role=Role.MEMBER, invited_by=user
        )
        decline_invitation(invitation=inv, user=other_user)
        row = WorkspaceAuditEvent.objects.get(action=AuditAction.INVITATION_DECLINED)
        assert row.actor_id == other_user.pk

    def test_a_suspension_has_a_reason_and_NO_actor(self, user, other_user):
        """A suspension is applied by a POLICY (the require-MFA sweep, the
        deactivation consumer), not by a person clicking. Naming an actor here
        would be an invention."""
        ws = create_workspace(user=user, name="Acme")
        member = WorkspaceMember.objects.create(
            workspace=ws,
            user=other_user,
            role=Role.MEMBER,
            accepted_at="2026-01-01T00:00:00Z",
        )
        suspend_member(member, reason="no_mfa", notify=False)
        row = WorkspaceAuditEvent.objects.get(action=AuditAction.MEMBER_SUSPENDED)
        assert row.actor_id is None
        assert row.subject_id == other_user.pk
        assert row.metadata["reason"] == "no_mfa"

        unsuspend_member(member, notify=False)
        assert AuditAction.MEMBER_UNSUSPENDED in _actions(ws)

    def test_removal_and_role_change_record_who_did_it(
        self, authed_client, user, other_user
    ):
        ws = create_workspace(user=user, name="Acme")
        WorkspaceMember.objects.create(
            workspace=ws,
            user=other_user,
            role=Role.MEMBER,
            accepted_at="2026-01-01T00:00:00Z",
        )
        changed = authed_client.patch(
            f"{BASE}/{ws.id}/members/{other_user.pk}",
            {"role": Role.VIEWER},
            format="json",
        )
        assert changed.status_code == 200, changed.content
        role_row = WorkspaceAuditEvent.objects.get(action=AuditAction.MEMBER_ROLE_CHANGED)
        assert role_row.actor_id == user.pk
        assert role_row.metadata == {"old_role": Role.MEMBER, "new_role": Role.VIEWER}

        removed = authed_client.delete(f"{BASE}/{ws.id}/members/{other_user.pk}")
        assert removed.status_code == 204, removed.content
        gone_row = WorkspaceAuditEvent.objects.get(action=AuditAction.MEMBER_REMOVED)
        assert gone_row.actor_id == user.pk
        assert gone_row.subject_id == other_user.pk
        # The membership row is gone; the record of its going is not.
        assert not WorkspaceMember.objects.filter(workspace=ws, user=other_user).exists()


@pytest.mark.django_db
class TestReading:
    def test_a_member_may_read_the_history(self, authed_client, user):
        ws = create_workspace(user=user, name="Acme")
        create_invitation(
            workspace=ws, email="new@acme.test", role=Role.MEMBER, invited_by=user
        )
        res = authed_client.get(f"{BASE}/{ws.id}/audit")
        assert res.status_code == 200, res.content
        items = res.json()["items"]
        assert [i["action"] for i in items] == [AuditAction.INVITATION_CREATED]
        assert items[0]["subject_email"] == "new@acme.test"

    def test_a_non_member_may_not(self, api_client, user, other_user):
        ws = create_workspace(user=user, name="Acme")
        create_invitation(
            workspace=ws, email="new@acme.test", role=Role.MEMBER, invited_by=user
        )
        api_client.force_authenticate(user=other_user)
        res = api_client.get(f"{BASE}/{ws.id}/audit")
        assert res.status_code == 403, res.content

    def test_a_malformed_user_filter_matches_NOBODY(self, authed_client, user):
        """Ignoring the filter would hand back the whole history under a
        request that asked for one person's — the loudest possible wrong
        answer."""
        ws = create_workspace(user=user, name="Acme")
        create_invitation(
            workspace=ws, email="new@acme.test", role=Role.MEMBER, invited_by=user
        )
        res = authed_client.get(f"{BASE}/{ws.id}/audit?user_id=not-a-uuid")
        assert res.status_code == 200
        assert res.json()["items"] == []

    def test_one_workspace_never_sees_another_s_history(
        self, authed_client, user, other_user
    ):
        mine = create_workspace(user=user, name="Mine")
        theirs = create_workspace(user=other_user, name="Theirs")
        create_invitation(
            workspace=theirs, email="x@theirs.test", role=Role.MEMBER, invited_by=other_user
        )
        res = authed_client.get(f"{BASE}/{mine.id}/audit")
        assert res.status_code == 200
        assert res.json()["items"] == []


def test_every_emitted_membership_event_has_an_audit_action():
    """THE GATE FOR THE FUTURE.

    The failure mode this module keeps hitting is a mechanism built and a
    consumer that never picks it up. Here it would be a new lifecycle
    transition that emits its comm event and writes no history — invisible
    until an admin asks a question the audit cannot answer.

    Matching by NAME rather than by a hand-kept list: an event called
    `workspace.member_frozen` would demand an action called `member_frozen`,
    and adding one without the other fails here.
    """
    emitted = {
        value.split(".", 1)[1]
        for name, value in vars(ws_events).items()
        if name.startswith("EVENT_WORKSPACE_") and isinstance(value, str)
    }
    # Two deliberate exclusions, each for a stated reason:
    #  * `personal.created` is not a membership transition — it is a workspace
    #    coming into existence, and it has no audit surface to appear on;
    #  * `member_password_reset` is an ACCOUNT action performed through the
    #    roster, already carried by stapel-auth's own security journal; a
    #    second copy here would be two records of one event that can disagree.
    emitted -= {"personal.created", "member_password_reset"}
    actions = {a.value for a in AuditAction}
    missing = sorted(e for e in emitted if e not in actions)
    assert not missing, (
        f"emitted but never recorded: {missing}. Add the AuditAction and the "
        "record_audit() call at the transition that emits it."
    )


@override_settings()
@pytest.mark.django_db
def test_a_failing_audit_never_fails_the_change(user, monkeypatch):
    """An audit line is a record OF the change, not a precondition FOR it.

    Verified by breaking the write on purpose: the invitation still goes out.
    A history with a visible gap beats a product that cannot invite anybody
    because its journal table is unhappy.
    """
    ws = create_workspace(user=user, name="Acme")

    def boom(*args, **kwargs):
        raise RuntimeError("no journal today")

    monkeypatch.setattr(WorkspaceAuditEvent.objects, "create", boom)
    invitation = create_invitation(
        workspace=ws, email="new@acme.test", role=Role.MEMBER, invited_by=user
    )
    assert invitation.pk is not None
