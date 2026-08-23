"""Subject-scoped erasure — what this module removes, and how it is counted.

stapel-gdpr 0.5.0 made the *subject* of an erasure a parameter: an owner
library is asked to erase everything it holds about one
``(subject_type, subject_key)`` pair and to say what it removed. This module
declares two subject types:

``account``
    The person. Memberships go, the invitations they sent that never became
    a membership go, the workspaces they own are moved to their terminal
    state, and the provisioning saga rows that name them keep the money
    trail while losing the person.

``workspace``
    The workspace itself, once its purge window has expired. The window is
    the erasure request: ``delete_workspace`` sets ``deleted_at`` and
    announces ``workspace.deleted`` so peers can clean up while the id is
    still resolvable; the request that arrives afterwards is the signal that
    the window is over, so the row and everything hanging off it go.

Both functions are idempotent by construction — they delete what matches and
report zero on a redelivery — and both return a ``counts`` dict, which is
the difference between an owner saying "it ran" and an owner saying what it
did.

The membership journal is deliberately NOT in these counts. Since 0.24 it is
a stream in the core event store (``AUDIT_STREAM``), whose retention is
governed by ``STAPEL_EVENTSTORE`` and whose only purge primitive is
time-based; a subject-scoped purge is a core capability this module cannot
invent one floor up. MODULE.md says so out loud rather than letting the
receipt imply a coverage it does not have.
"""
from __future__ import annotations

import logging

from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

#: The name this module answers to in ``STAPEL_GDPR["DATA_OWNERS"]``. One
#: name for the whole seam: the in-process provider's ``section``, the
#: ``owner`` on every receipt, and the ``owner`` in every probe answer.
GDPR_OWNER = "workspaces"

#: The subject types this module claims. An erasure for anything else is
#: not ours and gets no receipt — a receipt from an owner that erased
#: nothing is worse than silence, because the orchestrator counts it.
GDPR_SUBJECT_TYPES = ("account", "workspace")


@transaction.atomic
def delete_account_rows(user_id) -> dict[str, int]:
    """The row-destroying half of an account erasure.

    Kept separate from :func:`anonymize_account_rows` because the in-process
    ``GDPRProvider`` protocol calls the two in sequence and the predicates
    differ: what never became a membership is deleted, what did is kept as
    workspace history minus the person.

    Owned workspaces are moved to their terminal state, not destroyed: the
    workspace may hold other people's memberships and other modules' data,
    and deciding its fate is a workspace erasure (:func:`erase_workspace`),
    not a side effect of its owner leaving.
    """
    from .models import (
        Workspace,
        WorkspaceInvitation,
        WorkspaceMember,
        WorkspaceProvisionOperation,
    )

    memberships, _ = WorkspaceMember.objects.filter(user_id=user_id).delete()

    # Every invitation this user sent that never became a membership —
    # declined, revoked and expired ones included (never_accepted(), not
    # pending(): erasure is about PII left behind, not about what is live).
    invitations, _ = WorkspaceInvitation.objects.filter(
        invited_by_id=user_id,
    ).never_accepted().delete()

    # The provisioning saga is a money trail: a row may still owe credits
    # back, and destroying it would destroy the obligation. So the person
    # is removed from it and the accounting survives — the same rule
    # billing applies to a ledger.
    provisions = WorkspaceProvisionOperation.objects.filter(
        user_id=user_id,
    ).update(username="", user_id=None)

    workspaces = Workspace.objects.filter(
        owner_id=user_id, deleted_at__isnull=True,
    ).update(deleted_at=timezone.now())

    return {
        "memberships": int(memberships),
        "invitations_sent": int(invitations),
        "provision_operations_anonymized": int(provisions),
        "workspaces_soft_deleted": int(workspaces),
    }


@transaction.atomic
def anonymize_account_rows(user_id) -> dict[str, int]:
    """Keep the records, drop the person: accepted invitations lose their
    ``invited_by`` link and stay as workspace history."""
    from .models import WorkspaceInvitation

    anonymized = WorkspaceInvitation.objects.filter(
        invited_by_id=user_id,
    ).accepted().update(invited_by=None)
    return {"invitations_anonymized": int(anonymized)}


@transaction.atomic
def erase_account(user_id) -> dict[str, int]:
    """Erase everything this module holds about one account.

    Both halves, because the comm path has only this one call: an owner
    reached over ``gdpr.erasure.requested`` never gets the provider
    protocol's separate ``anonymize()``, and an account erasure that left
    the inviter link behind would not be one.
    """
    counts = delete_account_rows(user_id)
    counts.update(anonymize_account_rows(user_id))
    return counts


@transaction.atomic
def erase_workspace(workspace_id) -> dict[str, int]:
    """Erase everything this module holds about one workspace.

    Children are removed explicitly rather than through the FK cascade, for
    one reason: a receipt has to carry counts, and a cascade reports its
    collateral as one opaque number under someone else's label.

    The row itself goes last. It survived ``delete_workspace`` so that peers
    could resolve the id while they cleaned up their own workspace-scoped
    data; keeping it after the purge window would keep the workspace's name,
    slug, settings and owner link — which is the data we were asked to
    erase.
    """
    from .models import (
        Workspace,
        WorkspaceInvitation,
        WorkspaceMember,
        WorkspaceMFAEnforcement,
        WorkspaceProvisionOperation,
    )

    memberships, _ = WorkspaceMember.objects.filter(
        workspace_id=workspace_id,
    ).delete()
    invitations, _ = WorkspaceInvitation.objects.filter(
        workspace_id=workspace_id,
    ).delete()
    enforcements, _ = WorkspaceMFAEnforcement.objects.filter(
        workspace_id=workspace_id,
    ).delete()
    provisions, _ = WorkspaceProvisionOperation.objects.filter(
        workspace_id=workspace_id,
    ).delete()
    workspaces, _ = Workspace.objects.filter(id=workspace_id).delete()

    return {
        "memberships": int(memberships),
        "invitations": int(invitations),
        "mfa_enforcements": int(enforcements),
        "provision_operations": int(provisions),
        "workspaces": int(workspaces),
    }


#: subject_type -> the callable that erases it.
ERASERS = {
    "account": erase_account,
    "workspace": erase_workspace,
}


def erase_subject(subject_type: str, subject_key) -> dict[str, int]:
    """Erase one subject; raise :class:`KeyError` for a type we do not claim."""
    return ERASERS[subject_type](subject_key)
