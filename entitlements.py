"""Billing entitlement seam — the workspaces side (org-program spec §D2).

Entitlement answers "may the ORGANIZATION do this" (billing-keyed by the
workspace owner's subscription); capability answers "may the USER do this in
the organization" (``permissions.has_capability``). Semantically the checks
are distinguishable (402 vs 403) and a view runs the capability check first.

The seam is a comm Function, ``billing.check_entitlement``, owned by
stapel-billing. Without billing installed (OSS default) the two comm wiring
errors — ``FunctionNotRegistered`` / ``FunctionRouteNotConfigured`` — degrade
to ALLOW: an unbilled deployment is unrestricted by construction. A billing
that is installed but *failing* (``FunctionCallError``) is NOT swallowed:
payment enforcement is security-relevant, the error propagates to the caller.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from stapel_core.comm import call
from stapel_core.comm.exceptions import (
    FunctionNotRegistered,
    FunctionRouteNotConfigured,
)

logger = logging.getLogger(__name__)

#: Entitlement keys enforced by this module (catalog lives in billing plans).
ENT_ORG = "workspaces.org"
ENT_MEMBERS_MAX = "workspaces.members.max"
ENT_PROVISION_USER = "workspaces.provision_user"

CHECK_ENTITLEMENT = "billing.check_entitlement"
DEBIT = "billing.debit"
#: The compensating half of :data:`DEBIT`. stapel-billing does not publish
#: this Function yet, so the refund normally cannot be made from here — see
#: :func:`refund_provision_credits`, which is written so that "cannot" is a
#: recorded debt rather than a lost one.
CREDIT = "billing.credit"


@dataclass
class EntitlementResult:
    """Verdict of one entitlement check.

    Attributes:
        allowed: Whether the organization may proceed.
        limit: Plan limit for quantified keys (members.max); None otherwise.
        reason: Denial/degrade reason from billing (best-effort, may be None).
    """

    allowed: bool
    limit: int | None = None
    reason: str | None = None


#: OSS default: billing not installed → everything is allowed.
ALLOWED = EntitlementResult(allowed=True, reason="billing_not_installed")


class EntitlementDenied(Exception):
    """Raised by service-layer enforcement when billing denies an entitlement.

    Views translate it into the 402 error envelope
    (``error.402.member_limit_reached`` / ``error.402.entitlement_required``).
    """

    def __init__(self, key: str, result: EntitlementResult):
        self.key = key
        self.result = result
        super().__init__(f"entitlement {key!r} denied: {result.reason or 'limit'}")


def check_entitlement(user_id, key: str, *, quantity: int = 1) -> EntitlementResult:
    """Ask billing whether the subscription anchored on *user_id* allows *key*.

    ``quantity`` is the would-be total (billing knows the limit, the caller
    counts usage — spec §D1). Degrades to ALLOW when billing is absent.
    """
    try:
        result = call(
            CHECK_ENTITLEMENT,
            {"user_id": str(user_id), "key": key, "quantity": quantity},
        )
    except (FunctionNotRegistered, FunctionRouteNotConfigured):
        logger.debug(
            "billing.check_entitlement unavailable — allowing %r (OSS default)",
            key,
        )
        return ALLOWED
    result = result or {}
    return EntitlementResult(
        allowed=bool(result.get("allowed")),
        limit=result.get("limit"),
        reason=result.get("reason"),
    )


def check_org_entitlement(workspace, key: str, *, quantity: int = 1) -> EntitlementResult:
    """Entitlement check for an existing workspace.

    The billing anchor of an organization is its owner (spec §D1: billing is
    user-keyed; org-keyed subscriptions are a future refactor this seam does
    not block).
    """
    return check_entitlement(workspace.owner_id, key, quantity=quantity)


def debit_provision_credits(
    workspace, *, provision_id, username: str, credits: int
) -> None:
    """Charge the workspace owner for one provisioned org user (spec §D2).

    ``billing.debit`` with a deterministic idempotency key derived from the
    per-request provision UUID (``ws-provision:<uuid>``): comm delivery is
    at-least-once, and a transport-level retry of the same provisioning
    call must not debit twice. The username rides in ``metadata`` (audit),
    never any credential material.

    Degrades to a no-op when billing is not installed (OSS default, same
    two wiring errors as :func:`check_entitlement`). A billing that answers
    ``ok: false`` (e.g. insufficient credits) raises
    :class:`EntitlementDenied` — the view turns it into the 402 envelope.
    A billing that is installed but *failing* propagates, as everywhere in
    this module.
    """
    try:
        result = call(
            DEBIT,
            {
                "user_id": str(workspace.owner_id),
                "credits": int(credits),
                "idempotency_key": f"ws-provision:{provision_id}",
                "metadata": {
                    "workspace_id": str(workspace.id),
                    "username": username,
                    "action": "workspaces.provision_user",
                },
                "description": f"Provisioned org user {username}",
            },
        )
    except (FunctionNotRegistered, FunctionRouteNotConfigured):
        logger.debug(
            "billing.debit unavailable — provisioning uncharged (OSS default)"
        )
        return
    result = result or {}
    if not result.get("ok"):
        raise EntitlementDenied(
            ENT_PROVISION_USER,
            EntitlementResult(allowed=False, reason=result.get("reason")),
        )


def refund_provision_credits(
    workspace, *, operation_id, credits: int, reason: str = ""
) -> bool:
    """Give back what a failed provisioning charged (WORK-03).

    The compensating step of the provisioning saga. Returns True when
    billing accepted the refund, False when it could not be made — the
    caller keeps the debt on the operation row and
    ``manage.py reconcile_provisioning`` retries it, so an unrefundable
    charge is a queue with a number in it instead of a line in a log.

    Idempotency key mirrors the debit's, prefixed: replaying the refund of
    one operation is one refund.

    NOTE: ``billing.credit`` is not published by stapel-billing today, so
    the honest answer here is usually False. That is deliberate — silently
    treating "no refund seam" as "nothing owed" is how the orphan charge
    the audit found stayed invisible in the first place.
    """
    try:
        result = call(
            CREDIT,
            {
                "user_id": str(workspace.owner_id),
                "credits": int(credits),
                "idempotency_key": f"ws-provision-refund:{operation_id}",
                "metadata": {
                    "workspace_id": str(workspace.id),
                    "action": "workspaces.provision_user.refund",
                    "reason": reason,
                },
                "description": "Refund for a provisioning that did not complete",
            },
        )
    except (FunctionNotRegistered, FunctionRouteNotConfigured):
        logger.warning(
            "billing.credit unavailable — %s credits owed back to workspace %s "
            "for provisioning operation %s remain recorded for reconciliation",
            credits,
            workspace.pk,
            operation_id,
        )
        return False
    except Exception:
        logger.exception(
            "billing.credit failed for provisioning operation %s", operation_id
        )
        return False
    return bool((result or {}).get("ok"))


def member_seats_quantity(workspace, *, additional: int = 0) -> int:
    """Would-be seat total for ``workspaces.members.max``.

    Seats = members that hold one
    (:meth:`~stapel_workspaces.models.MembershipQuerySet.holds_seat`) + live
    pending invitations
    (:meth:`~stapel_workspaces.models.InvitationQuerySet.pending`) +
    *additional* new invitations. Pending invitations reserve seats so a
    burst of invites cannot overshoot the plan between send and accept; on
    accept the invitation is still pending at check time, i.e. already
    counted — callers pass ``additional=0`` there.

    Suspended members do NOT hold a seat (#92). A suspension is precisely
    "this membership stops counting for every check" — and the owner's own
    formula excludes them, so counting them here billed the org for people
    the product refuses to let in. It went unnoticed while suspensions were
    a rarity (the ``no_mfa`` policy); account deactivation makes them
    routine, and an admin deactivating a departed employee has to free the
    seat, not just the login. The row is still there and the seat is
    reclaimed on ``user.reactivated`` — that is the reversibility the
    suspended state is for.

    Neither half spells its predicate out here any more: both live on the
    model querysets, which is how the invitation half learned about
    ``declined_at`` (0.10.0). It had known about revocation and the TTL but
    not about the invitee's own "no", so a declined invitation kept a paid
    seat reserved until it expired.

    This function is the module's seat formula, not the owner's billing
    formula. It counts invitees who have never signed in, on purpose; the
    owner's "active user" definition does not. If those two are meant to
    agree, no test in this repository checks it.
    """
    seated = workspace.members.holds_seat().count()
    pending = workspace.invitations.pending().count()
    return seated + pending + additional
