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
