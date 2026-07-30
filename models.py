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
    created_at = models.DateTimeField(auto_now_add=True)

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
