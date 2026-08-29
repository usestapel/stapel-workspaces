"""Action subscriptions of the workspaces module.

Handlers must be idempotent: delivery is at-least-once (outbox retries,
broker redelivery).

The GDPR pair below — ``gdpr.erasure.requested`` and ``gdpr.owner.probe`` —
lives in this one file on purpose. The probe is answered from the same
subscriber that erases, so ``gdpr.owner.alive`` is evidence that the
erasure path is *consumed*, not merely that a container is deployed. Split
them and "alive" stops proving anything (stapel-gdpr MODULE.md, "Erasure
parts", option 2).
"""
import logging

from django.core.exceptions import ValidationError

from stapel_core.comm import on_action

from .erasure import GDPR_OWNER, GDPR_SUBJECT_TYPES

logger = logging.getLogger(__name__)

#: What a guest's personal workspace is renamed to when it is re-owned by the
#: account that absorbed the guest. It stops being *their* Personal space the
#: moment it belongs to somebody who already has one, so it must not keep a
#: name that claims to be it.
MERGED_GUEST_WORKSPACE_NAME = "Guest recordings"


class MergeTargetNotReady(RuntimeError):
    """A ``user.merged`` arrived before the surviving account exists here.

    Transient, not a bug: the guest has workspaces or memberships to carry
    over but there is no local user row to point their FKs at yet. Raising is
    the comm layer's retry signal — ``deliver()`` wraps a failing handler in
    ``ActionDeliveryError`` and the outbox redelivers — so the transfer
    completes once the survivor's user projection lands. An operator seeing
    this in a redelivery loop is looking at an ordering lag, not a defect.
    """


ERASURE_REQUESTED_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "gdpr.erasure.requested",
    "type": "object",
    "required": ["correlation_id", "subject_type", "subject_key"],
    "properties": {
        "request_id": {"type": "integer"},
        "correlation_id": {"type": "string"},
        "subject_type": {"type": "string"},
        "subject_key": {"type": "string"},
        "workspace_id": {"type": "string"},
        "requested_by": {"type": "string"},
        "origin": {"type": "string"},
        "due_at": {"type": "string"},
    },
    "additionalProperties": False,
}

OWNER_PROBE_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "gdpr.owner.probe",
    "type": "object",
    "required": ["correlation_id"],
    "properties": {"correlation_id": {"type": "string"}},
    "additionalProperties": False,
}


def _receipt(correlation_id, subject_type, subject_key, counts) -> None:
    """Emit ``gdpr.section.erased`` for work that has just committed.

    Callers hold an open transaction; the emit rides the outbox, so the
    receipt leaves iff the erasure commits. An owner that receipts a
    rollback is worse than an owner that stays silent — the orchestrator
    counts the receipt and finalizes.
    """
    from stapel_core.comm import emit

    emit(
        "gdpr.section.erased",
        {
            "correlation_id": str(correlation_id),
            "owner": GDPR_OWNER,
            "subject_type": subject_type,
            "subject_key": str(subject_key),
            "counts": counts,
        },
        key=str(subject_key),
    )


@on_action("gdpr.erasure.requested", schema=ERASURE_REQUESTED_SCHEMA)
def handle_erasure_requested(event):
    """Erase the named subject and confirm with counts (stapel-gdpr 0.5.0).

    A subject type this module does not claim is not ours to answer: the
    orchestrator creates a part only for owners that declared the type, and
    a receipt against a part that does not exist teaches it nothing.
    """
    from django.db import transaction

    from .erasure import erase_subject

    payload = event.payload
    subject_type = payload.get("subject_type")
    subject_key = payload.get("subject_key")
    correlation_id = payload.get("correlation_id")
    if subject_type not in GDPR_SUBJECT_TYPES:
        return
    if not subject_key or not correlation_id:
        logger.error(
            "malformed gdpr.erasure.requested: %s", getattr(event, "event_id", "?"),
        )
        return

    try:
        with transaction.atomic():
            counts = erase_subject(subject_type, subject_key)
            _receipt(correlation_id, subject_type, subject_key, counts)
    except (TypeError, ValueError, ValidationError):
        # An unparseable key names no row here. Receipting would claim an
        # erasure that never happened; raising would retry forever.
        logger.error(
            "gdpr.erasure.requested with unusable %s key %r [correlation=%s]",
            subject_type, subject_key, correlation_id,
        )
        return
    logger.info(
        "workspaces erased %s %s: %s [correlation=%s]",
        subject_type, subject_key, counts, correlation_id,
    )


@on_action("gdpr.owner.probe", schema=OWNER_PROBE_SCHEMA)
def handle_owner_probe(event):
    """Answer the liveness probe — see this module's docstring for why here."""
    from stapel_core.comm import emit

    emit(
        "gdpr.owner.alive",
        {
            "owner": GDPR_OWNER,
            "subject_types": list(GDPR_SUBJECT_TYPES),
            "correlation_id": str(event.payload.get("correlation_id") or ""),
        },
        key=GDPR_OWNER,
    )


@on_action("user.deleted")
def handle_user_deleted(event):
    """Erase this module's PII when an account deletion is executed.

    Deprecated upstream: stapel-gdpr emits ``user.deleted`` alongside
    ``gdpr.erasure.requested`` for account subjects until 0.6.0. Both land
    here, both run the same ``erase_subject("account", ...)`` and both
    receipt — the orchestrator's part flips once and ignores the second,
    so the two paths cannot drift into disagreeing about what was erased.

    GDPR erasure — irreversible and row-destroying. Deliberately NOT the
    same path as ``user.deactivated`` below (#92): an administrative
    deactivation must leave a suspended membership to come back to.
    """
    from django.db import transaction

    from .erasure import erase_account

    user_id = event.payload.get("user_id")
    if not user_id:
        logger.error("user.deleted event without user_id: %s", event.event_id)
        return
    correlation_id = event.payload.get("correlation_id")
    with transaction.atomic():
        counts = erase_account(user_id)
        if correlation_id:
            _receipt(correlation_id, "account", user_id, counts)
    logger.info(
        "workspaces data erased for deleted user %s: %s", user_id, counts,
    )


@on_action("user.merged")
def handle_user_merged(event):
    """Carry a merged-away guest's workspaces and memberships to the survivor.

    stapel-auth absorbs an anonymous guest into an existing account and then
    DELETES the guest row. ``WorkspaceMember.user`` is ``CASCADE`` and
    ``Workspace.owner`` is ``PROTECT``, so without this handler the guest's
    memberships vanish with them — and a guest who owns a workspace makes the
    deletion itself fail. Both are settled here, in one transaction, before
    that deletion lands.

    This is NOT a plain column rewrite; three domain rules apply.

    * **The guest's personal workspace is demoted.** "Personal" means *this
      person's own space*, and the survivor already has one. Re-owning it as
      a second PERSONAL row would leave two, and
      ``services.ensure_personal_workspace`` picks whichever comes first —
      so it lands as :attr:`~.models.WorkspaceType.WORK` (the only other
      kind this module offers) named :data:`MERGED_GUEST_WORKSPACE_NAME`.
      The recordings inside it keep their ``workspace_id`` and need no
      coordination; they are simply reachable again.
    * **Reassigned memberships lose ``is_preferred``.** At most one row per
      user may carry it (``workspaces_member_one_preferred_per_user``), and
      the survivor's own choice of home is theirs, not a guest session's.
    * **Memberships dedup against ``workspaces_member_unique``.** Both
      accounts may sit in the same workspace; the survivor's row is the one
      that stays, with its role, its accepted_at and its history. The
      guest's duplicate is dropped rather than reassigned into a constraint
      violation.

    Two different "unknown id" situations, and conflating them loses data:
    a guest who owns nothing here is a genuine no-op, returned quietly; a
    guest who owns rows while the survivor has no user row here yet raises
    :class:`MergeTargetNotReady` so the event is redelivered, because
    returning success would let the outbox mark it delivered and lose the
    workspaces for good.
    """
    from django.contrib.auth import get_user_model
    from django.db import transaction

    from .models import (
        Workspace,
        WorkspaceInvitation,
        WorkspaceMember,
        WorkspaceProvisionOperation,
        WorkspaceType,
    )

    payload = event.payload or {}
    from_user_id = payload.get("from_user_id")
    into_user_id = payload.get("into_user_id")
    if not from_user_id or not into_user_id:
        logger.error("user.merged without from/into user id: %s", event.event_id)
        return
    if str(from_user_id) == str(into_user_id):
        return

    with transaction.atomic():
        # Every read, and the decision they feed, happens inside the
        # transaction and before the first write, so the "not yet" path below
        # can never leave half the rows moved.
        try:
            guest_memberships = list(
                WorkspaceMember.objects.filter(user_id=from_user_id)
            )
            guest_workspaces = list(Workspace.objects.filter(owner_id=from_user_id))
            owns_provenance = (
                WorkspaceMember.objects.filter(invited_by_id=from_user_id).exists()
                or WorkspaceInvitation.objects.filter(
                    invited_by_id=from_user_id
                ).exists()
                or WorkspaceInvitation.objects.filter(
                    revoked_by_id=from_user_id
                ).exists()
                or WorkspaceProvisionOperation.objects.filter(
                    user_id=from_user_id
                ).exists()
            )
            survivor_workspace_ids = set(
                WorkspaceMember.objects.filter(user_id=into_user_id).values_list(
                    "workspace_id", flat=True
                )
            )
        except (ValidationError, ValueError, TypeError):
            logger.warning("user.merged with unusable user ids: %s", event.event_id)
            return
        if not (guest_memberships or guest_workspaces or owns_provenance):
            # Nothing to carry: the guest never reached this service, or a
            # previous delivery already moved everything. Quiet by design —
            # this is also the at-least-once idempotency path.
            return
        if not get_user_model().objects.filter(pk=into_user_id).exists():
            # The guest HAS rows but the survivor has no row here yet, so
            # nothing can point a FK at them. Raising is this comm layer's
            # retry signal, so the transfer lands once the survivor's user
            # projection arrives.
            raise MergeTargetNotReady(
                f"user.merged {from_user_id} -> {into_user_id}: the surviving "
                f"account has no user row in stapel-workspaces yet; redeliver "
                f"once its projection has landed"
            )

        moved_members = 0
        dropped_members = 0
        for membership in guest_memberships:
            if membership.workspace_id in survivor_workspace_ids:
                # The survivor is already in this workspace: their row wins,
                # role and history included. Reassigning would violate
                # workspaces_member_unique.
                membership.delete()
                dropped_members += 1
                continue
            membership.user_id = into_user_id
            membership.is_preferred = False
            membership.save(update_fields=["user", "is_preferred"])
            survivor_workspace_ids.add(membership.workspace_id)
            moved_members += 1

        moved_workspaces = 0
        demoted_workspaces = 0
        for workspace in guest_workspaces:
            workspace.owner_id = into_user_id
            fields = ["owner", "updated_at"]
            if workspace.type == WorkspaceType.PERSONAL:
                workspace.type = WorkspaceType.WORK
                workspace.name = MERGED_GUEST_WORKSPACE_NAME
                fields += ["type", "name"]
                demoted_workspaces += 1
            workspace.save(update_fields=fields)
            moved_workspaces += 1

        # Provenance columns: who invited, who revoked, whose provisioning
        # saga. No constraint is scoped to the user, so these are plain
        # rewrites.
        WorkspaceMember.objects.filter(invited_by_id=from_user_id).update(
            invited_by_id=into_user_id
        )
        WorkspaceInvitation.objects.filter(invited_by_id=from_user_id).update(
            invited_by_id=into_user_id
        )
        WorkspaceInvitation.objects.filter(revoked_by_id=from_user_id).update(
            revoked_by_id=into_user_id
        )
        WorkspaceProvisionOperation.objects.filter(user_id=from_user_id).update(
            user_id=into_user_id
        )

    logger.info(
        "user.merged %s -> %s: %s memberships moved, %s dropped as duplicates, "
        "%s workspaces re-owned (%s demoted from personal)",
        from_user_id,
        into_user_id,
        moved_members,
        dropped_members,
        moved_workspaces,
        demoted_workspaces,
    )


@on_action("user.mfa_disabled")
def handle_user_mfa_disabled(event):
    """The user lost their last STRONG second factor (auth emit, spec §C3).

    Suspend their membership in every workspace whose security policy
    requires MFA (reason ``no_mfa``, ``workspace.member_suspended`` emit +
    mfa_suspension letter per workspace). Idempotent: already-suspended
    memberships are skipped, so an at-least-once redelivery is a no-op.
    """
    from .services import suspend_memberships_without_mfa

    user_id = event.payload.get("user_id")
    if not user_id:
        logger.error("user.mfa_disabled event without user_id: %s", event.event_id)
        return
    suspended = suspend_memberships_without_mfa(user_id)
    if suspended:
        logger.info(
            "suspended %d require_mfa membership(s) for user %s",
            suspended,
            user_id,
        )


@on_action("user.mfa_enabled")
def handle_user_mfa_enabled(event):
    """The user gained their first STRONG second factor (auth emit, §C3).

    Lift their ``no_mfa`` suspensions — ONLY that reason; suspensions for
    other/future reasons are not MFA's to lift
    (``workspace.member_unsuspended`` emit + mfa_restored letter per
    workspace). Idempotent: active memberships are skipped on redelivery.
    """
    from .services import lift_no_mfa_suspensions_for_user

    user_id = event.payload.get("user_id")
    if not user_id:
        logger.error("user.mfa_enabled event without user_id: %s", event.event_id)
        return
    lifted = lift_no_mfa_suspensions_for_user(user_id)
    if lifted:
        logger.info(
            "lifted %d no_mfa suspension(s) for user %s", lifted, user_id
        )


@on_action("user.deactivated")
def handle_user_deactivated(event):
    """The ACCOUNT was administratively deactivated in auth (#92).

    Before this handler, deactivation reached exactly one place — auth's own
    session guard — so a deactivated user kept every membership, kept
    showing up in member lists, and kept costing the owner a seat.

    Suspend every membership the account holds (reason
    ``account_deactivated``): reversible, nothing deleted, the seat freed.
    Reversed by :func:`handle_user_reactivated`. Idempotent — a redelivery
    finds the memberships already suspended and does nothing, and in
    particular does not overwrite the first suspension's timestamp.
    """
    from .services import suspend_memberships_for_deactivated_user

    user_id = event.payload.get("user_id")
    if not user_id:
        logger.error("user.deactivated event without user_id: %s", event.event_id)
        return
    suspended = suspend_memberships_for_deactivated_user(user_id)
    if suspended:
        logger.info(
            "suspended %d membership(s) for deactivated user %s",
            suspended,
            user_id,
        )


@on_action("user.reactivated")
def handle_user_reactivated(event):
    """The account was restored in auth (#92) — undo the deactivation.

    Lifts ONLY the ``account_deactivated`` suspensions; a ``no_mfa``
    suspension belongs to the MFA consumer and stays. Without this handler
    the deactivation half would be a one-way door: the user logs back in and
    sees nothing.
    """
    from .services import lift_deactivation_suspensions_for_user

    user_id = event.payload.get("user_id")
    if not user_id:
        logger.error("user.reactivated event without user_id: %s", event.event_id)
        return
    lifted = lift_deactivation_suspensions_for_user(user_id)
    if lifted:
        logger.info(
            "lifted %d deactivation suspension(s) for user %s", lifted, user_id
        )
