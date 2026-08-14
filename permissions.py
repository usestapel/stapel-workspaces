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

    **The MFA door (WORK-01).** In a workspace whose ``require_mfa`` policy
    is on, a membership whose second factor has never been confirmed is not
    an admission: the member is asked about here, once, and the answer is
    stored. Before this, the policy was applied by a single sweep at the
    moment it was switched on — anyone who joined afterwards, or whom the
    sweep never reached because auth was down, walked in unchecked while
    the organization was told MFA was required.

    A workspace without the policy pays nothing for this: the settings
    block rides on the row already joined here.
    """
    qs = WorkspaceMember.objects.filter(
        workspace_id=workspace_id, user_id=user_id
    ).select_related("workspace")
    qs = qs.accepted() if include_suspended else qs.active()
    membership = qs.first()
    if membership is None:
        return None
    from .services import mfa_admission_blocked

    if mfa_admission_blocked(membership):
        return None
    return membership


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


def has_active_mandate(user) -> bool:
    """True if *user* holds at least one active mandate ANYWHERE.

    Active = accepted AND not suspended (:meth:`MembershipQuerySet.active`),
    in a workspace that has not been soft-deleted — the same filter
    ``WorkspaceListCreateView.get`` applies to a member's own workspace
    list, so this predicate and that endpoint always agree.

    Deliberately workspace-agnostic — it does not take a ``workspace_id``.
    A caller asking "may X act in WORKSPACE W" wants
    :func:`get_membership`/:func:`has_capability` instead; this one answers
    "does X hold a mandate ANYWHERE at all", which is a different question
    (a member of workspace A is still mandate-less in workspace B).
    """
    if getattr(user, "is_anonymous", False):
        return False
    return (
        WorkspaceMember.objects.active()
        .filter(user=user, workspace__deleted_at__isnull=True)
        .exists()
    )


def is_guest(user) -> bool:
    """True if *user* is a guest: authenticated with NO active mandate anywhere.

    THE canonical guest predicate (mandate-model vardict, 2026-08-03,
    developer's decision #1: guest is a STATE, "authenticated but without an
    active mandate", not a role). An anonymous session is guest by
    construction (:func:`has_active_mandate` is unconditionally False for
    it — it can hold no ``WorkspaceMember`` row at all); a REGISTERED
    account with zero accepted, non-suspended memberships gets the exact
    same answer, on purpose — the owner's decision was that both get "the
    same incomplete dashboard as Anonymous".

    Modeling this as a role was rejected during the vardict: a role needs a
    ``WorkspaceMember`` row to live on, and minting one for every guest
    would make guests count against seat billing, MFA suspension sweeps,
    member listings and GDPR erasure — machinery built for people who
    actually joined something. It also cannot express "member of org A,
    guest of org B's resource", because a role is absolute and guestness is
    relational (user × workspace). This predicate has neither problem: it
    reads existing rows and creates none.
    """
    return not has_active_mandate(user)
