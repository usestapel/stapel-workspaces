"""Permission helpers for workspaces.

Two layers (org-program spec §A2):

* **Roles** — ``role_at_least`` orders roles by the ``rank`` of the effective
  registry (``capabilities.effective_roles``). The builtin four keep their
  historical order (viewer < member < admin < owner) — the
  backward-compatibility gate of the 0.6 mandate model.
* **Capabilities** — ``has_capability`` / ``require_capability`` answer
  "may this user do <domain>.<action> in this workspace" against the same
  registry (wildcards included). Suspension-awareness lands in W3; until
  then "active membership" means accepted.
"""

from .capabilities import role_has_capability, role_rank
from .models import Role, WorkspaceMember

# Historical hierarchy of the builtin four (higher index = more powerful).
# Kept as a public name for backward compatibility; ordering now derives
# from the registry ranks (100/200/300/400) — same order by construction.
ROLE_HIERARCHY = [Role.VIEWER, Role.MEMBER, Role.ADMIN, Role.OWNER]


def role_at_least(role: str, minimum: str) -> bool:
    """True if *role* ranks at least *minimum* in the effective registry.

    Unknown roles (either side) → False, matching the pre-rank behavior for
    values outside the hierarchy. Custom registry roles participate via
    their ``rank``.
    """
    rank = role_rank(role)
    minimum_rank = role_rank(minimum)
    if rank is None or minimum_rank is None:
        return False
    return rank >= minimum_rank


def get_membership(workspace_id, user_id) -> WorkspaceMember | None:
    """Active membership of the user in the workspace (accepted only)."""
    return WorkspaceMember.objects.filter(
        workspace_id=workspace_id, user_id=user_id, accepted_at__isnull=False
    ).first()


def require_role(workspace_id, user_id, minimum: str) -> WorkspaceMember | None:
    """Return membership if user has at least `minimum` role, else None."""
    membership = get_membership(workspace_id, user_id)
    if membership and role_at_least(membership.role, minimum):
        return membership
    return None


def has_capability(workspace_id, user_id, capability: str) -> bool:
    """True if the user's active membership grants *capability*."""
    return require_capability(workspace_id, user_id, capability) is not None


def require_capability(workspace_id, user_id, capability: str) -> WorkspaceMember | None:
    """Return membership if the user holds *capability*, else None.

    Only accepted memberships count (suspension filtering arrives with the
    W3 suspension fields). Deny-by-default: no membership, unknown role, or
    a role without the capability all return None.
    """
    membership = get_membership(workspace_id, user_id)
    if membership and role_has_capability(membership.role, capability):
        return membership
    return None
