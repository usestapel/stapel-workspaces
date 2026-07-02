"""Service-layer helpers for workspaces (creation, invites)."""

import logging
from datetime import timedelta
from secrets import token_urlsafe

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify

from stapel_core.comm import emit
from stapel_core.django.workspaces import invalidate_membership_cache
from stapel_core.signals import workspace_member_changed

from .events import EVENT_WORKSPACE_PERSONAL_CREATED
from .models import Role, Workspace, WorkspaceInvitation, WorkspaceMember, WorkspaceType

logger = logging.getLogger(__name__)


def _make_unique_slug(name: str) -> str:
    base = slugify(name)[:48] or "workspace"
    candidate = base
    n = 1
    while Workspace.objects.filter(slug=candidate).exists():
        n += 1
        candidate = f"{base}-{n}"
    return candidate


@transaction.atomic
def create_workspace(*, user, name: str, slug: str | None = None, type: str = WorkspaceType.WORK) -> Workspace:
    """Create a workspace and seed the owner membership."""
    if not slug:
        slug = _make_unique_slug(name)
    ws = Workspace.objects.create(name=name, slug=slug, type=type, owner=user)
    WorkspaceMember.objects.create(
        workspace=ws, user=user, role=Role.OWNER, accepted_at=timezone.now()
    )
    # Transactional outbox: leaves iff this transaction commits.
    emit(
        "workspace.created",
        {
            "workspace_id": str(ws.id),
            "owner_id": str(user.pk),
            "name": ws.name,
            "type": ws.type,
        },
    )
    workspace_member_changed.send(
        sender=WorkspaceMember, workspace=ws, user=user, role=Role.OWNER, action="added"
    )
    return ws


def ensure_personal_workspace(user) -> Workspace:
    """Auto-create a Personal workspace on first login if one doesn't exist."""
    existing = Workspace.objects.filter(
        owner=user, type=WorkspaceType.PERSONAL, deleted_at__isnull=True
    ).first()
    if existing:
        return existing
    ws = create_workspace(user=user, name="Personal", type=WorkspaceType.PERSONAL)
    emit(
        EVENT_WORKSPACE_PERSONAL_CREATED,
        {"workspace_id": str(ws.id), "user_id": str(user.pk)},
    )
    emit(
        "workspace.member_joined",
        {"workspace_id": str(ws.id), "user_id": str(user.pk), "role": str(Role.OWNER)},
    )
    return ws


def create_invitation(*, workspace: Workspace, email: str, role: str, invited_by) -> WorkspaceInvitation:
    invitation = WorkspaceInvitation.objects.create(
        workspace=workspace,
        email=email.lower().strip(),
        role=role,
        invited_by=invited_by,
        token=token_urlsafe(32),
        expires_at=timezone.now() + timedelta(days=7),
    )
    _send_invitation_notification(invitation)
    return invitation


def _send_invitation_notification(invitation: WorkspaceInvitation) -> None:
    """Ask stapel-notifications to deliver the invite email.

    Best-effort: a delivery hiccup must never break invitation creation —
    the invite stays listable/resendable either way.
    """
    try:
        from stapel_core.django.users.models import User
        from stapel_core.notifications import request_notification

        path = f"/invitations/{invitation.token}/accept"
        frontend_url = (getattr(settings, "FRONTEND_URL", "") or "").rstrip("/")
        accept_url = f"{frontend_url}{path}" if frontend_url else path

        inviter = invitation.invited_by
        inviter_name = ""
        if inviter is not None:
            inviter_name = (
                (inviter.get_full_name() or "").strip()
                or inviter.username
                or inviter.email
                or ""
            )

        invitee = User.objects.filter(email__iexact=invitation.email).first()
        target = (
            {"user_id": str(invitee.pk)}
            if invitee is not None
            else {"email": invitation.email}
        )
        request_notification(
            "workspace.invitation",
            variables={
                "workspace_name": invitation.workspace.name,
                "inviter_name": inviter_name,
                "accept_url": accept_url,
            },
            source_service="workspaces",
            **target,
        )
    except Exception:
        logger.exception(
            "failed to request invitation notification for %s", invitation.pk
        )


@transaction.atomic
def accept_invitation(*, invitation: WorkspaceInvitation, user) -> WorkspaceMember:
    # Lock the invitation row: a single-use token must not be consumable
    # twice by concurrent requests.
    locked = (
        WorkspaceInvitation.objects.select_for_update()
        .filter(pk=invitation.pk, accepted_at__isnull=True)
        .first()
    )
    if locked is None:
        raise ValueError("invitation already used")
    locked.accepted_at = timezone.now()
    locked.save(update_fields=["accepted_at"])
    member, _ = WorkspaceMember.objects.get_or_create(
        workspace=locked.workspace,
        user=user,
        defaults={"role": locked.role, "accepted_at": timezone.now()},
    )
    # Subscribers must be idempotent (at-least-once delivery), so emitting
    # again for an already-existing membership is safe.
    emit(
        "workspace.member_joined",
        {
            "workspace_id": str(locked.workspace_id),
            "user_id": str(user.pk),
            "role": str(member.role),
        },
    )
    # A negative membership lookup may be cached cross-service; drop it.
    invalidate_membership_cache(locked.workspace_id, user.pk)
    workspace_member_changed.send(
        sender=WorkspaceMember,
        workspace=locked.workspace,
        user=user,
        role=member.role,
        action="added",
    )
    return member
