"""Action subscriptions of the workspaces module.

Handlers must be idempotent: delivery is at-least-once (outbox retries,
broker redelivery).
"""
import logging

from stapel_core.comm import on_action

logger = logging.getLogger(__name__)


@on_action("user.deleted")
def handle_user_deleted(event):
    """Erase this module's PII when an account deletion is executed.

    GDPR erasure — irreversible and row-destroying. Deliberately NOT the
    same path as ``user.deactivated`` below (#92): an administrative
    deactivation must leave a suspended membership to come back to.
    """
    from .gdpr import WorkspacesGDPRProvider

    user_id = event.payload.get("user_id")
    if not user_id:
        logger.error("user.deleted event without user_id: %s", event.event_id)
        return
    WorkspacesGDPRProvider().delete(user_id)
    logger.info("workspaces data erased for deleted user %s", user_id)


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
