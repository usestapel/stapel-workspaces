"""
Workspace + membership model.

stapel-workspaces is the foundational service: every workspace-scoped
resource in other services carries `workspace_id` FK pointing at the
Workspace row owned here.
"""

import uuid

from django.conf import settings
from django.db import models

from stapel_core.access.declaration import access


#: Canonical suspension reason: the workspace requires strong MFA and the
#: member has none (org-program spec §C3). The only reason the MFA-event
#: consumer lifts automatically.
SUSPENSION_NO_MFA = "no_mfa"

#: Suspension reason: the ACCOUNT was administratively deactivated in auth
#: (``user.deactivated``, #92). Lifted only by ``user.reactivated`` — each
#: reason is owned by the consumer that set it and lifted by nobody else,
#: so a user who is both MFA-less and deactivated does not walk back into a
#: require_mfa workspace just because their account was restored.
SUSPENSION_ACCOUNT_DEACTIVATED = "account_deactivated"


class WorkspaceType(models.TextChoices):
    PERSONAL = "personal", "Personal"
    WORK = "work", "Work"


class Role(models.TextChoices):
    """The BUILTIN four roles.

    The effective role set is extensible via ``STAPEL_WORKSPACES["ROLES"]``
    (see ``capabilities.effective_roles``); ``choices`` on the role columns
    stay declared for the builtin values (admin/display defaults — the
    stapel-recordings ``SourceType`` precedent for extensible enums) while
    serializers validate against the effective registry, so custom product
    roles are storable.
    """

    OWNER = "owner", "Owner"
    ADMIN = "admin", "Admin"
    MEMBER = "member", "Member"
    VIEWER = "viewer", "Viewer"


class Workspace(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=64, unique=True)
    type = models.CharField(
        max_length=16, choices=WorkspaceType.choices, default=WorkspaceType.PERSONAL
    )
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="owned_workspaces",
    )
    settings = models.JSONField(default=dict, blank=True)
    storage_used_bytes = models.BigIntegerField(default=0)
    storage_limit_bytes = models.BigIntegerField(default=5 * 1024 * 1024 * 1024)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "workspaces_workspace"
        indexes = [
            models.Index(fields=["owner"]),
            models.Index(fields=["type"]),
            models.Index(fields=["deleted_at"]),
        ]

    def __str__(self):
        return f"{self.name} ({self.type})"


class MemberState(models.TextChoices):
    """The membership lifecycle, spelled out (#92).

    Derived, never stored — the timestamps are the truth; this enum only
    stops three genuinely different situations from being read off the same
    two nullable columns by eye.

    ``invited``
        Row exists, ``accepted_at`` is NULL — the invitation has not been
        taken up yet. Reserves a seat (see
        :func:`~stapel_workspaces.entitlements.member_seats_quantity`).
    ``active``
        Accepted and not suspended. The only state that counts for access
        checks and for the seat bill.
    ``suspended``
        Accepted but ``suspended_at`` is set — REVERSIBLE. The row, the
        role and the history all stay; the membership simply stops counting
        (``no_mfa`` from the require_mfa policy, ``account_deactivated``
        from auth's ``user.deactivated``). Lifting the suspension restores
        the membership exactly as it was.
    ``deleted``
        The row is **gone**. Reached only by an explicit removal or by the
        GDPR erasure path (``user.deleted`` →
        :meth:`~stapel_workspaces.gdpr.WorkspacesGDPRProvider.delete`) —
        irreversible, and never produced by a suspension. It is in this
        enum precisely so that "deactivated" can never be mistaken for it:
        an administrative deactivation must leave a *suspended* membership
        to come back to, and a GDPR erasure must not leave one at all.
        :attr:`WorkspaceMember.state` cannot return it — there is no row
        left to ask.
    """

    INVITED = "invited", "Invited"
    ACTIVE = "active", "Active"
    SUSPENDED = "suspended", "Suspended"
    DELETED = "deleted", "Deleted"


class MembershipQuerySet(models.QuerySet):
    """The only place the membership lifecycle columns are spelled out.

    The lifecycle is two nullable timestamps (``accepted_at``,
    ``suspended_at``) that have to be read TOGETHER; before this class the
    combination was hand-written at nine call sites and only some of them
    knew about suspension, so every new lifecycle column silently
    invalidated a subset of the copies. One of those copies was the seat
    count and it billed organizations for people the product refuses to let
    in (#92, fixed in 0.9.0).

    ``tests/test_lifecycle_predicates.py`` fails the build if
    ``accepted_at__isnull`` / ``suspended_at__isnull`` reappear in a
    ``filter()`` anywhere outside this module.

    **What that rule does not buy.** Nothing here knows whether these
    formulas are the RIGHT ones. It prevents the *second* drift — copies
    that stop agreeing with each other — after a human has once translated
    the specification into columns. The *first*, semantic divergence is
    caught only by a spec-derived test or an end-to-end scenario, and this
    module has neither. Concretely, the owner's "active user" formula
    (registered AND activated this month; never-signed-in excluded) is
    **not** what :meth:`active` computes — ``last_accessed_at`` is not
    consulted here at all, and a live invitation reserves a seat although
    its invitee has never signed in. If the two are supposed to agree, that
    agreement is currently nobody's test.
    """

    def active(self):
        """Memberships that may act: accepted AND not suspended.

        The access predicate — comm Functions, the internal membership
        endpoint, ``permissions.get_membership``, the member's own
        workspace list and the suspension sweeps all mean exactly this.
        Suspension is not removal (org-program spec §C3): the row and the
        role stay, they simply stop counting until the suspension lifts.
        """
        return self.filter(accepted_at__isnull=False, suspended_at__isnull=True)

    def accepted(self):
        """Memberships that were taken up, **suspended ones included**.

        NOT an authorization predicate — deliberately so. This is the
        lookup for surfaces that must be able to SEE a suspended row in
        order to report it honestly (the view layer answering
        ``error.403.membership_suspended`` instead of a bare
        not-a-member 403). An authorization decision that reaches for this
        instead of :meth:`active` is a bug.
        """
        return self.filter(accepted_at__isnull=False)

    def suspended(self, reason=None):
        """Suspended memberships, optionally narrowed to one *reason*.

        Each reason is owned by the consumer that set it: the MFA consumer
        lifts ``no_mfa`` and nothing else, the deactivation consumer lifts
        ``account_deactivated`` and nothing else. Passing *reason* is how
        that ownership is spelled — an unfiltered lift would walk an
        MFA-less user back into a require_mfa workspace.
        """
        qs = self.filter(suspended_at__isnull=False)
        if reason is not None:
            qs = qs.filter(suspension_reason=reason)
        return qs

    def holds_seat(self):
        """Memberships that occupy a billable seat (``workspaces.members.max``).

        A separate name from :meth:`active` on purpose, and the same rows
        as :meth:`active` on purpose. "May act" and "costs money" are two
        different questions that happen to share an answer today; while
        they shared a hand-copied *spelling* instead, the answers drifted
        apart and the bill counted suspended members (#92). Whoever makes
        them differ edits this body and writes the reason here, so the
        divergence is a decision on the record rather than a discovery on
        an invoice.

        This is only the membership half of the seat total: live pending
        invitations reserve seats too — see
        :func:`stapel_workspaces.entitlements.member_seats_quantity`.
        """
        return self.active()


class WorkspaceMember(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(
        Workspace, on_delete=models.CASCADE, related_name="members"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="workspace_memberships",
    )
    role = models.CharField(max_length=32, choices=Role.choices, default=Role.MEMBER)
    invited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="sent_workspace_invitations",
    )
    invited_at = models.DateTimeField(auto_now_add=True)
    accepted_at = models.DateTimeField(null=True, blank=True)
    last_accessed_at = models.DateTimeField(null=True, blank=True)
    #: Org-created (synthetic) member — joined via POST members/provision
    #: rather than an invitation (org-program spec §C1). Audit/metering flag.
    provisioned = models.BooleanField(default=False)
    #: Suspension is NOT removal (org-program spec §C3): the row (and the
    #: role) stays, but the membership stops counting for every access
    #: check while suspended_at is set. ``suspension_reason`` is an open
    #: vocabulary; the canonical values are ``no_mfa`` (require_mfa policy
    #: enforcement — :data:`SUSPENSION_NO_MFA`) and ``account_deactivated``
    #: (auth's ``user.deactivated`` —
    #: :data:`SUSPENSION_ACCOUNT_DEACTIVATED`). See :class:`MemberState` for
    #: why suspension and deletion are kept apart.
    suspended_at = models.DateTimeField(null=True, blank=True)
    suspension_reason = models.CharField(max_length=32, blank=True, default="")
    #: A NAME HINT, not the canonical name — the name lives in stapel-profiles
    #: (this module never grows its own copy of that field). Copied once, at
    #: creation, from ``WorkspaceInvitation.display_name_hint`` (the invite's
    #: "Name" field) or ``ProvisionMemberRequest.display_name``; never touched
    #: again by this module. ``MemberResponse.display_name`` prefers a live
    #: lookup in stapel-profiles and falls back to this only when profiles has
    #: no name yet for the user — so the moment the person sets their own name
    #: there, this stops being read. Max length mirrors stapel-profiles'
    #: ``Profile.display_name`` (35) so a hint is never silently truncated on
    #: the day it lands there for real.
    display_name_hint = models.CharField(max_length=35, blank=True, default="")
    #: The person's EXPLICIT choice of home workspace — the choice
    #: ``STAPEL_WORKSPACES["DEFAULT_WORKSPACE_ID"]`` already promises to yield
    #: to ("a DEFAULT, not a cage: a person still switches spaces, and their
    #: explicit choice wins over it") and which, until now, had nowhere to be
    #: recorded. At most one row per user carries it; the partial unique
    #: constraint below is the enforcement, not a convention.
    #:
    #: Deliberately a flag on the MEMBERSHIP rather than a column on a
    #: user-level row, because that makes it self-healing: remove the member
    #: and the preference leaves with the row — no cleanup job, no dangling
    #: pointer at a workspace the person can no longer open. Suspension is
    #: reversible and leaves the row, so the flag survives it while
    #: :meth:`MembershipQuerySet.active` simply stops echoing it, and it
    #: comes back when the suspension lifts.
    #:
    #: NOT :attr:`last_accessed_at`. That column is telemetry written as a
    #: side effect of a GET; reading it as "the active workspace" is exactly
    #: what produced "the owner cannot see his own invitations" (#239). A
    #: choice is stated, never inferred from where somebody last clicked.
    is_preferred = models.BooleanField(default=False)

    #: Carries the canonical lifecycle predicates (see
    #: :class:`MembershipQuerySet`) onto both ``WorkspaceMember.objects``
    #: and the related manager ``workspace.members``.
    objects = MembershipQuerySet.as_manager()

    @property
    def state(self) -> str:
        """This membership's :class:`MemberState` (derived, see that enum).

        Never :attr:`MemberState.DELETED` — that state is the absence of
        the row.
        """
        if self.suspended_at is not None:
            return MemberState.SUSPENDED
        if self.accepted_at is None:
            return MemberState.INVITED
        return MemberState.ACTIVE

    class Meta:
        db_table = "workspaces_member"
        constraints = [
            models.UniqueConstraint(
                fields=["workspace", "user"], name="workspaces_member_unique"
            ),
            # "At most one preferred workspace per user" is an invariant of
            # the data, so it is stated to the database. Two devices switching
            # at the same moment would otherwise both write a flag and the
            # answer to "where is home" would depend on row order.
            models.UniqueConstraint(
                fields=["user"],
                condition=models.Q(is_preferred=True),
                name="workspaces_member_one_preferred_per_user",
            ),
        ]
        indexes = [
            models.Index(fields=["user"]),
            models.Index(fields=["workspace", "role"]),
        ]

    def __str__(self):
        return f"{self.user_id} @ {self.workspace_id} ({self.role})"


class InvitationStatus(models.TextChoices):
    """Externally visible invitation states (org-program spec §B2).

    Derived, not stored — see :attr:`WorkspaceInvitation.status`.
    """

    PENDING = "pending", "Pending"
    ACCEPTED = "accepted", "Accepted"
    DECLINED = "declined", "Declined"
    REVOKED = "revoked", "Revoked"
    EXPIRED = "expired", "Expired"


class InvitationQuerySet(models.QuerySet):
    """The only place the invitation lifecycle columns are spelled out.

    Same discipline as :class:`MembershipQuerySet`, same reason: the
    invitation state is *four* columns plus the clock, and the hand-written
    copies had already stopped agreeing — the seat count knew about
    ``revoked_at`` and the TTL but not about ``declined_at`` (added with the
    decline flow in 0.7), so an invitation the invitee had explicitly
    refused went on reserving a paid seat until it expired. The ban in
    ``tests/test_lifecycle_predicates.py`` covers these columns too.
    """

    def pending(self):
        """Live, actionable invitations — exactly :attr:`InvitationStatus.PENDING`.

        No terminal timestamp set AND the TTL has not run out. This is the
        "reserves a seat" set: an invitation that can still turn into a
        membership, and nothing else.
        """
        from django.utils import timezone

        return self.filter(
            accepted_at__isnull=True,
            declined_at__isnull=True,
            revoked_at__isnull=True,
            expires_at__gt=timezone.now(),
        )

    def unresolved(self):
        """No terminal timestamp set — deliberately **clock-free**.

        The compare-and-set target for accept/decline: those transitions
        re-read the row under ``select_for_update`` and must fail if any
        terminal state got there first. Expiry is left out because the TTL
        is validated at the view boundary, where it maps to its own error
        key (``error.400.invitation_expired``) instead of a bare "already
        used"; a row-lock filter must not silently swallow that
        distinction.
        """
        return self.filter(
            accepted_at__isnull=True,
            declined_at__isnull=True,
            revoked_at__isnull=True,
        )

    def accepted(self):
        """Invitations that were taken up (a membership exists for them)."""
        return self.filter(accepted_at__isnull=False)

    def never_accepted(self):
        """Invitations that never produced a membership — for ANY reason.

        Wider than the complement of :meth:`pending`: declined, revoked and
        expired rows are in here too. Used by the GDPR erasure path, where
        the question is "did this ever become a membership", not "is it
        still live" — an erasure that spared declined rows would leave PII
        behind.
        """
        return self.filter(accepted_at__isnull=True)


@access.secret  # bearer invite token: superuser-only, token masked in admin (AS-3)
class WorkspaceInvitation(models.Model):
    """Pending invite by email — resolved into WorkspaceMember on acceptance.

    State machine (three timestamps + TTL; ``status`` derives the label):

        pending ──accept──▶ accepted   (terminal; membership created)
        pending ──decline─▶ declined   (terminal; invitee said no)
        pending ──revoke──▶ revoked    (terminal; the org withdrew it)
        pending ──(time)──▶ expired    (expires_at elapsed; nothing stored)

    ``declined`` ≠ ``revoked``: decline is the invitee's action, revoke is
    the workspace's — both stay distinguishable in ``status``. A claim
    (login-grant mint for an unregistered email) does NOT transition the
    invitation: accept remains a separate, deliberate step after account
    setup.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(
        Workspace, on_delete=models.CASCADE, related_name="invitations"
    )
    email = models.EmailField()
    role = models.CharField(max_length=32, choices=Role.choices, default=Role.MEMBER)
    invited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )
    token = models.CharField(max_length=64, unique=True)
    expires_at = models.DateTimeField()
    accepted_at = models.DateTimeField(null=True, blank=True)
    declined_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    #: WHO withdrew it — the other half of ``revoked_at``, and the same
    #: provenance shape :attr:`invited_by` already uses for the opposite
    #: transition (FK, ``SET_NULL``, never a copied name string): the actor
    #: is a row this service can join to, and the record survives that
    #: account being deleted as an honest "somebody, no longer known".
    #:
    #: Until 0.23 the actor existed ONLY in the ``workspace.invitation_revoked``
    #: event payload, so a workspace could show *when* an invitation was
    #: withdrawn and never *by whom* — an audit gap on a permissioned action,
    #: since the emitted event is a fire-and-forget message on a bus, not a
    #: record this service can answer a question from. The event still
    #: carries it; this column is what the API can read back.
    revoked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="revoked_workspace_invitations",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    #: When a letter for this invitation was last handed to the mailer —
    #: written by ``services._send_invitation_notification`` on every
    #: successful ``request_notification``, i.e. on creation AND on every
    #: resend. NULL means "no letter was ever sent" (rows predating 0.23, and
    #: any deployment with no notifications service at all).
    #:
    #: This is the resend cooldown's clock
    #: (``STAPEL_WORKSPACES["INVITATION_RESEND_COOLDOWN_SECONDS"]``). Before
    #: it, resend had no cooldown and no record of the previous send at all:
    #: one admin holding ``members.invite`` could drive an unbounded number
    #: of letters at one address through this fleet's mail infrastructure.
    last_sent_at = models.DateTimeField(null=True, blank=True)
    #: The invite modal's "Name" field (a NAME HINT, not the canonical name —
    #: see ``WorkspaceMember.display_name_hint``, which this is copied onto at
    #: accept time). Optional: an invite without one behaves exactly as
    #: before. Max length mirrors stapel-profiles' ``Profile.display_name``.
    display_name_hint = models.CharField(max_length=35, blank=True, default="")

    #: Canonical lifecycle predicates (see :class:`InvitationQuerySet`) on
    #: both ``WorkspaceInvitation.objects`` and ``workspace.invitations``.
    objects = InvitationQuerySet.as_manager()

    class Meta:
        db_table = "workspaces_invitation"
        indexes = [
            models.Index(fields=["email"]),
            models.Index(fields=["workspace", "accepted_at"]),
        ]

    @property
    def status(self) -> str:
        """Derived state label — the precedence mirrors the accept-time
        validation order (revoked beats accepted beats declined beats the
        TTL): a stored terminal timestamp always wins over mere passage of
        time, so an accepted-then-expired invite reads ``accepted``."""
        from django.utils import timezone

        if self.revoked_at:
            return InvitationStatus.REVOKED
        if self.accepted_at:
            return InvitationStatus.ACCEPTED
        if self.declined_at:
            return InvitationStatus.DECLINED
        if self.expires_at and self.expires_at < timezone.now():
            return InvitationStatus.EXPIRED
        return InvitationStatus.PENDING
