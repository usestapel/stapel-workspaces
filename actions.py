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
