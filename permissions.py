"""Permission helpers for workspaces.

Two layers (org-program spec §A2):

* **Roles** — ``role_at_least`` orders roles by the ``rank`` of the effective
  registry (``capabilities.effective_roles``). The builtin four keep their
  historical order (viewer < member < admin < owner) — the
  backward-compatibility gate of the 0.6 mandate model.
* **Capabilities** — ``has_capability`` / ``require_capability`` answer
  "may this user do <domain>.<action> in this workspace" against the same
  registry (wildcards included). "Active membership" means accepted AND
  not suspended (org-program spec §C3): suspension is not removal — the
  row stays, but it stops counting for every access check until lifted.
  Callers that must SEE a suspended row (the view layer, to answer
  ``error.403.membership_suspended`` instead of a bare not-a-member 403)
  pass ``include_suspended=True`` explicitly.
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


def get_membership(
    workspace_id, user_id, *, include_suspended: bool = False
) -> WorkspaceMember | None:
    """Active membership of the user in the workspace.

    Active = accepted AND not suspended (spec §C3). ``include_suspended``
    widens the lookup to suspended rows — for surfaces that must
    distinguish "suspended member" from "not a member" (the view layer's
    ``membership_suspended`` 403); authorization decisions never pass it.
    """
    qs = WorkspaceMember.objects.filter(
        workspace_id=workspace_id, user_id=user_id, accepted_at__isnull=False
    )
    if not include_suspended:
        qs = qs.filter(suspended_at__isnull=True)
    return qs.first()


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

    Only accepted, non-suspended memberships count (spec §C3). Deny-by-
    default: no membership, a suspended membership, an unknown role, or a
    role without the capability all return None.
    """
    membership = get_membership(workspace_id, user_id)
    if membership and role_has_capability(membership.role, capability):
        return membership
    return None
