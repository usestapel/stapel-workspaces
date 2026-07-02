"""Stapel Workspaces — team workspaces and RBAC Django app for Stapel.

Public API (see ``__all__``):

Service functions (``stapel_workspaces.services``):
- ``create_workspace`` — create a workspace and seed the owner membership.
- ``ensure_personal_workspace`` — get-or-create a user's personal workspace.
- ``create_invitation`` — invite an email address to a workspace.
- ``accept_invitation`` — resolve an invitation into a membership.

comm Function provider (``stapel_workspaces.functions``):
- ``CHECK_MEMBERSHIP`` — name of the ``workspaces.check_membership``
  Function (call it via ``stapel_core.comm.call``).
- ``check_membership`` — the provider itself.

Events (``stapel_workspaces.events``):
- ``EVENT_WORKSPACE_PERSONAL_CREATED`` — comm action name emitted when a
  personal workspace is bootstrapped.

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
    "CHECK_MEMBERSHIP": ".functions",
    "check_membership": ".functions",
    "EVENT_WORKSPACE_PERSONAL_CREATED": ".events",
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
