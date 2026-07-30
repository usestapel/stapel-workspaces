"""Role → capability registry (org-program spec §A1).

The mandate model lives in settings, not the database: this module ships the
builtin four roles as code, and a host project extends or overrides them via
``STAPEL_WORKSPACES["ROLES"]`` (merge-registry, last-wins per role key — the
same canon as ``STAPEL_NOTIFICATIONS["TYPES"]``). Capability strings are
namespaced ``"<domain>.<action>"`` and free-form for the client
(``meetings.spotlight``, ``records.view``, ...); the matcher supports the
``"*"`` wildcard and prefix wildcards like ``"members.*"``.

Not to be confused with ``_capabilities.py`` — the capabilities.json codegen
shim (module capability manifest, an unrelated concept).

Invariants (enforced by ``checks.py`` at startup and defensively here):

* ``owner`` cannot be overridden or removed — last-owner protection and
  "only an owner grants owner" stay hardcoded on that role;
* every entry carries an integer ``rank`` (ordering for ``role_at_least``)
  and a ``capabilities`` list of strings.
"""
from __future__ import annotations

from .conf import workspaces_settings

#: Builtin roles (org-program spec §A1). ``rank`` orders roles for
#: :func:`stapel_workspaces.permissions.role_at_least`; the builtin four keep
#: their historical order (viewer < member < admin < owner).
BUILTIN_ROLES: dict[str, dict] = {
    "owner": {"rank": 400, "capabilities": ["*"]},
    "admin": {"rank": 300, "capabilities": [
        "workspace.view", "workspace.update",
        "members.view", "members.invite", "members.remove",
        "members.role.change", "members.provision",
        "members.password.reset",
        "workspace.security.manage",
    ]},
    "member": {"rank": 200, "capabilities": ["workspace.view", "members.view"]},
    "viewer": {"rank": 100, "capabilities": ["workspace.view", "members.view"]},
}

#: Builtin capability step-up levels (org-program spec §A3). Everything not
#: listed is "standard"; endpoints executing a "high" capability are gated by
#: ``stapel_core.verification`` step-up (scope "sensitive") — wired in W3.
BUILTIN_CAPABILITY_LEVELS: dict[str, str] = {
    "members.provision": "high",
    # Resetting somebody else's password hands their account to whoever
    # ordered it (#110). Same level as minting an account outright: an
    # ambient session must not be enough, a fresh step-up must be.
    "members.password.reset": "high",
    "workspace.security.manage": "high",
}


def effective_roles() -> dict[str, dict]:
    """Effective role registry: builtin + ``STAPEL_WORKSPACES["ROLES"]`` overlay.

    Last-wins per role key — an overlay entry replaces the builtin entry
    whole (no deep merge). ``owner`` is system-protected: the builtin entry
    always wins (the system check reports a misconfigured overlay; this is
    the runtime backstop).
    """
    overlay = workspaces_settings.ROLES or {}
    roles = {**BUILTIN_ROLES, **overlay}
    roles["owner"] = BUILTIN_ROLES["owner"]
    return roles


def effective_capability_levels() -> dict[str, str]:
    """Effective capability→level map: builtin + settings overlay (last-wins)."""
    overlay = workspaces_settings.CAPABILITY_LEVELS or {}
    return {**BUILTIN_CAPABILITY_LEVELS, **overlay}


def capability_level(capability: str) -> str:
    """Step-up level of *capability*: ``"standard"`` (default) or ``"high"``."""
    return effective_capability_levels().get(capability, "standard")


def capability_matches(capability: str, granted: str) -> bool:
    """Match one *granted* capability string against the requested one.

    Supports the registry wildcards: ``"*"`` (everything) and prefix
    wildcards like ``"members.*"``. Mirrored by the consumer helper in
    ``stapel_core.django.workspaces`` — keep semantics in sync.
    """
    if granted == "*" or granted == capability:
        return True
    if granted.endswith(".*"):
        return capability.startswith(granted[:-1])
    return False


def capabilities_for(role: str) -> list[str]:
    """Granted capability strings of *role* (raw, wildcards included).

    Unknown role → empty list (deny-by-default).
    """
    entry = effective_roles().get(role)
    if not entry:
        return []
    return list(entry.get("capabilities", []))


def role_has_capability(role: str, capability: str) -> bool:
    """True if *role* grants *capability* per the effective registry."""
    return any(capability_matches(capability, g) for g in capabilities_for(role))


def role_rank(role: str) -> int | None:
    """Rank of *role* in the effective registry, or None for unknown roles."""
    entry = effective_roles().get(role)
    if not entry:
        return None
    rank = entry.get("rank")
    return rank if isinstance(rank, int) else None
