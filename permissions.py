"""Permission helpers for workspaces."""

from .models import Role, WorkspaceMember

# Hierarchy: higher index = more powerful
ROLE_HIERARCHY = [Role.VIEWER, Role.MEMBER, Role.ADMIN, Role.OWNER]


def role_at_least(role: str, minimum: str) -> bool:
    try:
        return ROLE_HIERARCHY.index(role) >= ROLE_HIERARCHY.index(minimum)
    except ValueError:
        return False


def get_membership(workspace_id, user_id) -> WorkspaceMember | None:
    return WorkspaceMember.objects.filter(
        workspace_id=workspace_id, user_id=user_id, accepted_at__isnull=False
    ).first()


def require_role(workspace_id, user_id, minimum: str) -> WorkspaceMember | None:
    """Return membership if user has at least `minimum` role, else None."""
    membership = get_membership(workspace_id, user_id)
    if membership and role_at_least(membership.role, minimum):
        return membership
    return None
