"""Action subscriptions of the workspaces module.

Handlers must be idempotent: delivery is at-least-once (outbox retries,
broker redelivery).
"""
import logging

from stapel_core.comm import on_action

logger = logging.getLogger(__name__)


@on_action("user.deleted")
def handle_user_deleted(event):
    """Erase this module's PII when an account deletion is executed."""
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
