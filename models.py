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
    #: Whether this member was PROVEN to hold a strong second factor, and
    #: when (org-program spec §C3 / WORK-01). Three values, and the third
    #: is the one that matters: True = auth answered yes, False = auth
    #: answered no (the membership is suspended for ``no_mfa``), NULL =
    #: **nobody has asked yet**.
    #:
    #: The policy used to have no third value. A sweep that stopped at the
    #: first auth error left the rest of the org untouched and
    #: indistinguishable from an org that had passed — the endpoint said
    #: "MFA is required now" and half the members had never been checked.
    #: NULL under a ``require_mfa`` policy is therefore not admission: see
    #: ``permissions.get_membership``, which verifies on the spot and
    #: refuses while the answer is unknown.
    mfa_compliant = models.BooleanField(null=True, blank=True, default=None)
    mfa_verified_at = models.DateTimeField(null=True, blank=True)
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
    #: When a login grant was last minted for this invitation, and how many
    #: have been (WORK-03). The claim endpoint mints an auth login grant for
    #: an address with no account yet; before this, a pending invitation
    #: could be claimed again and again, so one leaked invite link was an
    #: unbounded supply of session-bearing grants for that mailbox — each
    #: single-use in auth, and each mintable afresh here.
    #:
    #: One live grant at a time: a second claim inside the grant's TTL is
    #: refused (``error.429.invitation_grant_pending``), and after the TTL a
    #: genuine "I lost the email" retry still works. Counting them is what
    #: makes the abuse visible afterwards.
    login_grant_issued_at = models.DateTimeField(null=True, blank=True)
    login_grant_count = models.PositiveIntegerField(default=0)
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
        constraints = [
            # ONE live invitation per address per workspace, stated to the
            # database rather than trusted to the callers. Two unresolved
            # rows for one address are two working tokens for one person
            # and two reserved seats on the bill — and the second row was
            # one concurrent invite batch away, since the seat count is
            # read before the rows are written. Terminal rows (accepted,
            # declined, revoked) are outside the condition: the history of
            # everyone who was ever invited stays whole, and a fresh invite
            # after any terminal state is a fresh row.
            #
            # An EXPIRED-but-unresolved row is inside it on purpose:
            # `services.create_invitation` lands on that row and refreshes
            # its TTL instead of inserting a twin, which is what "invite
            # them again" means.
            models.UniqueConstraint(
                fields=["workspace", "email"],
                condition=models.Q(
                    accepted_at__isnull=True,
                    declined_at__isnull=True,
                    revoked_at__isnull=True,
                ),
                name="workspaces_invitation_one_live_per_email",
            ),
        ]
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


class AuditAction(models.TextChoices):
    """What happened to a workspace's membership.

    A CLOSED vocabulary on purpose. An audit whose action is a free string is
    a log: nobody can filter it, translate it, or notice that a new lifecycle
    transition shipped without a line. ``tests/test_audit.py`` fails the build
    if this list and the module's emitted events ever disagree.

    The lines themselves live in the core event store (stream
    ``STAPEL_WORKSPACES["AUDIT_STREAM"]`` — see ``audit.py``), not in a
    table here: the vocabulary is this module's contract, the storage is the
    platform's.
    """

    INVITATION_CREATED = "invitation_created", "Invitation created"
    INVITATION_ACCEPTED = "invitation_accepted", "Invitation accepted"
    INVITATION_REVOKED = "invitation_revoked", "Invitation revoked"
    INVITATION_DECLINED = "invitation_declined", "Invitation declined"
    #: The account itself came into existence through this invitation — the
    #: claim path, where somebody with no account at all joins. Distinct from
    #: INVITATION_ACCEPTED, which an existing account also performs: "a new
    #: person appeared in the world" and "a known person joined us" are
    #: different facts and the owner asked to track both.
    ACCOUNT_CREATED_BY_INVITATION = "account_created_by_invitation", "Account created by invitation"
    MEMBER_JOINED = "member_joined", "Member joined the organization"
    MEMBER_PROVISIONED = "member_provisioned", "Member provisioned by an admin"
    MEMBER_REMOVED = "member_removed", "Member removed"
    MEMBER_ROLE_CHANGED = "member_role_changed", "Member role changed"
    MEMBER_SUSPENDED = "member_suspended", "Member suspended"
    MEMBER_UNSUSPENDED = "member_unsuspended", "Member reinstated"
    #: The workspace itself ended. Not a membership transition, but it belongs
    #: in the same journal: it is the last line every membership above leads
    #: to, and a history that stops without saying the org was closed reads as
    #: a history that was truncated.
    DELETED = "deleted", "Workspace deleted"


class MFAEnforcementState(models.TextChoices):
    """How far a workspace's ``require_mfa`` policy has actually got.

    The policy used to be a boolean in a JSON blob and a best-effort sweep
    that returned True or False into a caller that ignored it: an
    administrator who turned MFA on got a 200 whether every member had been
    checked, half of them had, or auth had been unreachable from the first
    call onward (WORK-01).

    ``pending``
        The policy is on and no sweep has run yet.
    ``enforcing``
        A sweep has run but coverage is incomplete — members remain whose
        factor nobody has confirmed (a new joiner, an attempt that stopped
        at an auth error).
    ``enforced``
        Every active member has been asked and answered: compliant, or
        suspended for ``no_mfa``. This is the only state in which the
        workspace may be reported as enforcing MFA.
    ``failed``
        The last attempt hit an auth error. Distinct from ``enforcing``
        because it carries ``last_error`` and is what the retry sweep and
        the administrator's screen are looking for.
    """

    PENDING = "pending", "Pending"
    ENFORCING = "enforcing", "Enforcing"
    ENFORCED = "enforced", "Enforced"
    FAILED = "failed", "Failed"


class WorkspaceMFAEnforcement(models.Model):
    """The durable state of one workspace's ``require_mfa`` enforcement.

    One row per workspace, created when the policy is switched on and kept
    afterwards (a workspace that turned MFA off and on again keeps its
    history of attempts). It exists so three questions have answers that
    survive the request that asked them: has the sweep finished, when was
    it last tried, and what did auth say when it did not.

    :func:`~stapel_workspaces.services.retry_mfa_enforcement` is the
    durable half — an idempotent sweep over every row that is not
    ``enforced`` — and ``manage.py enforce_workspace_mfa`` is how a
    deployment schedules it.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.OneToOneField(
        Workspace, on_delete=models.CASCADE, related_name="mfa_enforcement"
    )
    state = models.CharField(
        max_length=16,
        choices=MFAEnforcementState.choices,
        default=MFAEnforcementState.PENDING,
    )
    #: When the policy was last switched on (the clock a compliance report
    #: measures "how long has this org been half-enforced" against).
    requested_at = models.DateTimeField(auto_now_add=True)
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    #: When coverage first became complete. Cleared whenever it stops being.
    completed_at = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveIntegerField(default=0)
    #: The last auth failure, verbatim, for the administrator's screen. Never
    #: a credential — ``auth.mfa_status`` answers with a boolean and errors
    #: with a message about the call, not about the factor.
    last_error = models.TextField(blank=True, default="")
    #: Coverage of the last attempt: members asked, and of those, members
    #: without a strong factor (i.e. suspended for no_mfa).
    checked_members = models.PositiveIntegerField(default=0)
    noncompliant_members = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = "workspaces_mfa_enforcement"
        indexes = [models.Index(fields=["state"])]

    def __str__(self):
        return f"{self.workspace_id}: mfa {self.state}"


class ProvisionState(models.TextChoices):
    """Where one provisioning operation got to (WORK-03).

    Provisioning spends money in billing, mints an account in auth and
    writes a membership here — three services, no shared transaction. It
    used to be a straight line with no record, so a failure anywhere left
    an orphan nobody could find: a charge with no account, or an account
    with no membership and no way to tell it from a half-finished retry.

    The states are what a compensating saga needs to be resumable:
    ``started`` (nothing external yet), ``charged``, ``account_created``,
    ``completed``, and the two the failure path uses — ``compensating``
    (something is owed back) and ``compensated``/``failed`` (settled).
    """

    STARTED = "started", "Started"
    CHARGED = "charged", "Charged"
    ACCOUNT_CREATED = "account_created", "Account created"
    COMPLETED = "completed", "Completed"
    COMPENSATING = "compensating", "Compensating"
    COMPENSATED = "compensated", "Compensated"
    FAILED = "failed", "Failed"


class WorkspaceProvisionOperation(models.Model):
    """One provisioning attempt, keyed by a stable operation id.

    The id is derived from (workspace, username) unless the caller supplies
    one, so a retry of the same provisioning IS the same operation: it does
    not charge twice, and once it has completed it answers with the member
    it already made instead of a second account.

    The row also outlives the request, which is the point — a charge that
    could not be refunded is a ``compensating`` row a human or
    ``manage.py reconcile_provisioning`` can act on, rather than a log line.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(
        Workspace, on_delete=models.CASCADE, related_name="provision_operations"
    )
    #: Idempotency key: the caller's, or uuid5(workspace, username).
    operation_id = models.CharField(max_length=64)
    username = models.CharField(max_length=255)
    state = models.CharField(
        max_length=20, choices=ProvisionState.choices, default=ProvisionState.STARTED
    )
    #: The auth account, once it exists — what a reconciliation needs to
    #: find an orphan account whose membership never landed.
    user_id = models.UUIDField(null=True, blank=True)
    credits = models.PositiveIntegerField(default=0)
    #: Which attempt of this operation is running. A resume (the process
    #: died mid-flight) keeps the number; a retry after a compensated
    #: failure raises it, so the fresh charge is a fresh charge and not a
    #: duplicate billing would rightly dedupe away.
    attempt = models.PositiveIntegerField(default=1)
    #: Credits still owed back to the org. Non-zero means somebody paid for
    #: a provisioning that did not happen.
    credits_to_refund = models.PositiveIntegerField(default=0)
    last_error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "workspaces_provision_operation"
        constraints = [
            models.UniqueConstraint(
                fields=["workspace", "operation_id"],
                name="workspaces_provision_operation_unique",
            ),
        ]
        indexes = [models.Index(fields=["state"])]

    def __str__(self):
        return f"{self.username}: {self.state}"
