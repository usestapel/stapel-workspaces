"""Stapel Workspaces — team workspaces and RBAC Django app for Stapel.

Public API (see ``__all__``):

Service functions (``stapel_workspaces.services``):
- ``create_workspace`` — create a workspace and seed the owner membership.
- ``ensure_personal_workspace`` — get-or-create a user's personal workspace.
- ``create_invitation`` — invite an email address to a workspace.
- ``accept_invitation`` — resolve an invitation into a membership.
- ``decline_invitation`` — the invitee's terminal "no" (≠ revoke).
- ``issue_invitation_login_grant`` — mint an ``auth.issue_login_grant``
  token for a not-yet-registered invitee (claim step, 0.7).

comm Function providers (``stapel_workspaces.functions``):
- ``CHECK_MEMBERSHIP`` — name of the ``workspaces.check_membership``
  Function (call it via ``stapel_core.comm.call``).
- ``check_membership`` — the provider itself.
- ``CHECK_CAPABILITY`` / ``check_capability`` — the
  ``workspaces.check_capability`` Function (mandate model, 0.6).

Mandate model (``stapel_workspaces.capabilities`` / ``.permissions``):
- ``effective_roles`` — builtin roles + ``STAPEL_WORKSPACES["ROLES"]``
  overlay (last-wins merge-registry).
- ``capabilities_for`` / ``role_has_capability`` — role → capability lookup
  (wildcards ``"*"`` / ``"prefix.*"`` supported).
- ``has_capability`` / ``require_capability`` — in-service checks against a
  user's accepted membership.

Entitlement seam (``stapel_workspaces.entitlements``):
- ``check_org_entitlement`` — ask billing whether the org's plan allows a
  key; degrades to allow when billing is not installed.

Events (``stapel_workspaces.events``):
- ``EVENT_WORKSPACE_PERSONAL_CREATED`` — comm action name emitted when a
  personal workspace is bootstrapped.
- ``EVENT_WORKSPACE_MEMBER_REMOVED`` / ``EVENT_WORKSPACE_MEMBER_ROLE_CHANGED``
  — member lifecycle emits for business services (kick, role change).

GDPR:
- ``WorkspacesGDPRProvider`` — export/delete provider for workspace data.

Signal usage (``workspace_member_changed``) stays in ``stapel_core.signals``.

All exports are lazily imported (PEP 562): importing ``stapel_workspaces``
itself does not require Django to be configured.
"""

_EXPORTS = {
    "create_workspace": ".services",
    "ensure_personal_workspace": ".services",
    "create_invitation": ".services",
    "accept_invitation": ".services",
    "decline_invitation": ".services",
    "issue_invitation_login_grant": ".services",
    "CHECK_MEMBERSHIP": ".functions",
    "check_membership": ".functions",
    "CHECK_CAPABILITY": ".functions",
    "check_capability": ".functions",
    "effective_roles": ".capabilities",
    "capabilities_for": ".capabilities",
    "role_has_capability": ".capabilities",
    "has_capability": ".permissions",
    "require_capability": ".permissions",
    "check_org_entitlement": ".entitlements",
    "EntitlementResult": ".entitlements",
    "EVENT_WORKSPACE_PERSONAL_CREATED": ".events",
    "EVENT_WORKSPACE_MEMBER_REMOVED": ".events",
    "EVENT_WORKSPACE_MEMBER_ROLE_CHANGED": ".events",
    "WorkspacesGDPRProvider": ".gdpr",
}

__all__ = list(_EXPORTS)


def __getattr__(name):
    if name in _EXPORTS:
        import importlib

        value = getattr(importlib.import_module(_EXPORTS[name], __name__), name)
        globals()[name] = value  # cache for subsequent lookups
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():
    return sorted(set(globals()) | set(__all__))
