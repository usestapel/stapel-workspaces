"""``user.merged`` — a guest's workspaces survive signing in.

stapel-auth absorbs an anonymous guest into an existing account and then
DELETES the guest row. ``WorkspaceMember.user`` is ``CASCADE`` and
``Workspace.owner`` is ``PROTECT``, so without this consumer the guest's
memberships disappear with them and a guest who owns a workspace makes the
deletion itself fail. What is pinned here:

* the guest's PERSONAL workspace is re-owned AND demoted — the survivor ends
  up with exactly one personal space, and the guest's recordings (which keep
  their ``workspace_id``) are reachable again from a work workspace named
  "Guest recordings";
* a reassigned membership loses ``is_preferred`` — at most one row per user
  may carry it (``workspaces_member_one_preferred_per_user``), and the
  survivor's own choice of home is not a guest session's to overwrite;
* a membership in a workspace the survivor is ALREADY in is dropped, not
  reassigned into ``workspaces_member_unique``, and the survivor's row keeps
  its role;
* the provenance columns (``invited_by``, ``revoked_by``, the provisioning
  saga's ``user_id``) follow too;
* the handler is idempotent, and a no-op for ids it has never seen;
* a guest with rows to carry and a survivor this service has not projected
  yet RAISES rather than reporting success, so the event is redelivered
  instead of the transfer being silently discarded.
"""
import types
import uuid

import pytest
from django.utils import timezone

from stapel_core.comm import emit
from stapel_core.comm.exceptions import ActionDeliveryError
from stapel_core.django.users.models import User

from stapel_workspaces.actions import (
    MERGED_GUEST_WORKSPACE_NAME,
    MergeTargetNotReady,
    handle_user_merged,
)
from stapel_workspaces.models import (
    Role,
    Workspace,
    WorkspaceInvitation,
    WorkspaceMember,
    WorkspaceProvisionOperation,
    WorkspaceType,
)
from stapel_workspaces.services import create_workspace, ensure_personal_workspace

pytestmark = pytest.mark.django_db


@pytest.fixture
def guest(db):
    """A guest session's user — what ``POST /auth/api/v1/anonymous/`` mints."""
    return User.create_anonymous_user()


def _payload(from_user_id, into_user_id):
    return {
        "from_user_id": str(from_user_id),
        "into_user_id": str(into_user_id),
        "reason": "anonymous_promotion",
    }


def _merge(from_user, into_user):
    """Deliver through the real subscription (in-process comm)."""
    emit(
        "user.merged",
        _payload(getattr(from_user, "id", from_user),
                 getattr(into_user, "id", into_user)),
    )


def _invitation(workspace, *, invited_by=None, revoked_by=None, email="x@example.com"):
    return WorkspaceInvitation.objects.create(
        workspace=workspace,
        email=email,
        role=Role.MEMBER,
        invited_by=invited_by,
        revoked_by=revoked_by,
        revoked_at=timezone.now() if revoked_by else None,
        token=uuid.uuid4().hex,
        expires_at=timezone.now() + timezone.timedelta(days=7),
    )


# ── the personal workspace ──────────────────────────────────────────────


def test_guest_personal_workspace_is_re_owned_and_demoted(guest, user):
    """Two personal workspaces for one person is the bug this rule prevents:
    ``ensure_personal_workspace`` would then answer with whichever row came
    first, so which space is "yours" would depend on row order."""
    guest_ws = ensure_personal_workspace(guest)
    survivor_ws = ensure_personal_workspace(user)

    _merge(guest, user)

    guest_ws.refresh_from_db()
    assert guest_ws.owner_id == user.id
    assert guest_ws.type == WorkspaceType.WORK
    assert guest_ws.name == MERGED_GUEST_WORKSPACE_NAME
    # The survivor's own personal space is untouched, and still the only one.
    survivor_ws.refresh_from_db()
    assert survivor_ws.type == WorkspaceType.PERSONAL
    assert survivor_ws.name == "Personal"
    assert (
        Workspace.objects.filter(owner=user, type=WorkspaceType.PERSONAL).count() == 1
    )
    # And the person can still open it: the membership came along.
    assert WorkspaceMember.objects.filter(workspace=guest_ws, user=user).exists()
    assert not WorkspaceMember.objects.filter(user=guest).exists()


def test_a_non_personal_workspace_is_re_owned_unchanged(guest, user):
    ws = create_workspace(user=guest, name="Guest's team")

    _merge(guest, user)

    ws.refresh_from_db()
    assert ws.owner_id == user.id
    assert ws.type == WorkspaceType.WORK
    assert ws.name == "Guest's team"  # only PERSONAL is renamed


# ── memberships ─────────────────────────────────────────────────────────


def test_reassigned_membership_loses_the_preferred_flag(guest, user):
    """``workspaces_member_one_preferred_per_user`` is a database constraint:
    a blind reassignment of a preferred guest row would raise."""
    guest_ws = ensure_personal_workspace(guest)
    guest_member = WorkspaceMember.objects.get(workspace=guest_ws, user=guest)
    guest_member.is_preferred = True
    guest_member.save(update_fields=["is_preferred"])

    survivor_ws = ensure_personal_workspace(user)
    survivor_member = WorkspaceMember.objects.get(workspace=survivor_ws, user=user)
    survivor_member.is_preferred = True
    survivor_member.save(update_fields=["is_preferred"])

    _merge(guest, user)

    moved = WorkspaceMember.objects.get(workspace=guest_ws, user=user)
    assert moved.is_preferred is False
    survivor_member.refresh_from_db()
    assert survivor_member.is_preferred is True  # the survivor's home stays


def test_duplicate_membership_is_dropped_and_the_survivor_row_wins(guest, user):
    """Both sat in the same workspace. ``workspaces_member_unique`` forbids
    two rows, and the survivor's is the one with the role and the history."""
    ws = create_workspace(user=user, name="Acme")  # user joins as OWNER
    WorkspaceMember.objects.create(workspace=ws, user=guest, role=Role.VIEWER)

    _merge(guest, user)  # would raise IntegrityError on a blind .update()

    rows = list(WorkspaceMember.objects.filter(workspace=ws))
    assert len(rows) == 1
    assert rows[0].user_id == user.id
    assert rows[0].role == Role.OWNER


def test_colliding_and_non_colliding_memberships_in_one_merge(guest, user):
    shared = create_workspace(user=user, name="Shared")
    guest_only = create_workspace(user=guest, name="Guest only")
    WorkspaceMember.objects.create(workspace=shared, user=guest, role=Role.VIEWER)

    _merge(guest, user)

    assert set(
        WorkspaceMember.objects.filter(user=user).values_list("workspace_id", flat=True)
    ) == {shared.id, guest_only.id}
    assert not WorkspaceMember.objects.filter(user=guest).exists()


# ── provenance columns ──────────────────────────────────────────────────


def test_provenance_columns_follow_the_survivor(guest, user, other_user):
    ws = create_workspace(user=user, name="Acme")
    invited = WorkspaceMember.objects.create(
        workspace=ws, user=other_user, role=Role.MEMBER, invited_by=guest
    )
    sent = _invitation(ws, invited_by=guest, email="a@example.com")
    withdrawn = _invitation(ws, revoked_by=guest, email="b@example.com")
    operation = WorkspaceProvisionOperation.objects.create(
        workspace=ws, operation_id=uuid.uuid4().hex, username="bot", user_id=guest.id
    )

    _merge(guest, user)

    invited.refresh_from_db()
    sent.refresh_from_db()
    withdrawn.refresh_from_db()
    operation.refresh_from_db()
    assert invited.invited_by_id == user.id
    assert sent.invited_by_id == user.id
    assert withdrawn.revoked_by_id == user.id
    assert operation.user_id == user.id


def test_provenance_alone_is_enough_to_be_worth_carrying(guest, user):
    """A guest owning no workspace and no membership can still be named as an
    inviter — that is rows to move, not a no-op."""
    ws = create_workspace(user=user, name="Acme")
    sent = _invitation(ws, invited_by=guest)

    _merge(guest, user)

    sent.refresh_from_db()
    assert sent.invited_by_id == user.id


# ── idempotency and no-ops ──────────────────────────────────────────────


def test_second_delivery_changes_nothing(guest, user):
    guest_ws = ensure_personal_workspace(guest)
    ensure_personal_workspace(user)

    _merge(guest, user)
    before = sorted(
        WorkspaceMember.objects.filter(user=user).values_list("id", "workspace_id")
    )
    _merge(guest, user)  # at-least-once delivery

    assert sorted(
        WorkspaceMember.objects.filter(user=user).values_list("id", "workspace_id")
    ) == before
    guest_ws.refresh_from_db()
    assert guest_ws.name == MERGED_GUEST_WORKSPACE_NAME
    assert Workspace.objects.filter(owner=user).count() == 2


def test_guest_owning_nothing_is_a_clean_no_op(guest, user):
    ensure_personal_workspace(user)
    _merge(guest, user)
    assert Workspace.objects.filter(owner=user).count() == 1


def test_merge_into_self_is_a_no_op(guest):
    ws = ensure_personal_workspace(guest)
    _merge(guest, guest)
    ws.refresh_from_db()
    assert ws.type == WorkspaceType.PERSONAL
    assert ws.owner_id == guest.id


def test_missing_ids_are_reported_and_ignored(guest, user):
    ws = ensure_personal_workspace(guest)

    handle_user_merged(
        types.SimpleNamespace(payload={"into_user_id": str(user.id)}, event_id="e1")
    )
    handle_user_merged(
        types.SimpleNamespace(payload={"from_user_id": str(guest.id)}, event_id="e2")
    )
    handle_user_merged(types.SimpleNamespace(payload={}, event_id="e3"))

    ws.refresh_from_db()
    assert ws.owner_id == guest.id


def test_unusable_user_ids_are_a_clean_no_op(user):
    """A key that cannot address a row here names nothing — say so quietly
    rather than starting a redelivery loop over a malformed payload."""
    handle_user_merged(
        types.SimpleNamespace(payload=_payload("not-a-uuid", user.id), event_id="e4")
    )
    assert not Workspace.objects.exists()


# ── the survivor has not been projected here yet ────────────────────────


def test_unknown_survivor_raises_and_moves_nothing(guest):
    """The guest HAS rows: returning success would let the outbox mark the
    event delivered and lose the workspace forever. Raise so it redelivers."""
    guest_ws = ensure_personal_workspace(guest)
    survivor_id = uuid.uuid4()

    with pytest.raises(ActionDeliveryError) as excinfo:
        emit("user.merged", _payload(guest.id, survivor_id))

    (cause,) = excinfo.value.errors
    assert isinstance(cause, MergeTargetNotReady)
    # An operator staring at a redelivery loop can name both accounts.
    assert str(guest.id) in str(cause) and str(survivor_id) in str(cause)

    # Nothing half-moved: a redelivery finds the rows intact under the guest.
    guest_ws.refresh_from_db()
    assert guest_ws.owner_id == guest.id
    assert guest_ws.type == WorkspaceType.PERSONAL
    assert WorkspaceMember.objects.filter(user=guest).count() == 1


def test_redelivery_after_the_survivor_appears_completes_the_transfer(guest):
    """The raise is a real retry path, not just a louder failure."""
    guest_ws = ensure_personal_workspace(guest)
    survivor_id = uuid.uuid4()

    with pytest.raises(ActionDeliveryError):
        emit("user.merged", _payload(guest.id, survivor_id))

    # The survivor's user projection lands...
    survivor = User.objects.create(id=survivor_id, username="late")

    emit("user.merged", _payload(guest.id, survivor_id))  # ...and it redelivers.

    guest_ws.refresh_from_db()
    assert guest_ws.owner_id == survivor.id
    assert guest_ws.type == WorkspaceType.WORK
    assert WorkspaceMember.objects.filter(workspace=guest_ws, user=survivor).exists()


def test_unknown_survivor_with_an_empty_guest_stays_quiet(guest):
    """No rows to carry — a genuine no-op, and the retry loop must not start."""
    emit("user.merged", _payload(guest.id, uuid.uuid4()))
    assert not Workspace.objects.exists()


def test_second_delivery_after_a_completed_merge_never_raises(guest, user):
    """Post-merge the guest owns nothing, so redelivery takes the quiet path
    even though the guest row itself may be long gone."""
    guest_ws = ensure_personal_workspace(guest)

    _merge(guest, user)
    _merge(guest, user)  # must not raise MergeTargetNotReady

    guest_ws.refresh_from_db()
    assert guest_ws.owner_id == user.id
