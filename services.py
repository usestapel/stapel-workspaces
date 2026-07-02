"""Service-layer helpers for workspaces (creation, invites)."""

from datetime import timedelta
from secrets import token_urlsafe

from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify

from .models import Role, Workspace, WorkspaceInvitation, WorkspaceMember, WorkspaceType


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
    return ws


def ensure_personal_workspace(user) -> Workspace:
    """Auto-create a Personal workspace on first login if one doesn't exist."""
    existing = Workspace.objects.filter(
        owner=user, type=WorkspaceType.PERSONAL, deleted_at__isnull=True
    ).first()
    if existing:
        return existing
    return create_workspace(user=user, name="Personal", type=WorkspaceType.PERSONAL)


def create_invitation(*, workspace: Workspace, email: str, role: str, invited_by) -> WorkspaceInvitation:
    return WorkspaceInvitation.objects.create(
        workspace=workspace,
        email=email.lower().strip(),
        role=role,
        invited_by=invited_by,
        token=token_urlsafe(32),
        expires_at=timezone.now() + timedelta(days=7),
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
    return member
