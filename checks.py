"""Django system checks for stapel-workspaces configuration.

Policy (docs/library-standard.md §3.7): E-level for configuration the
service cannot run with. The role registry drives authorization, so a
malformed ``STAPEL_WORKSPACES["ROLES"]`` overlay must block deploys rather
than silently deny (or worse, grant) capabilities at runtime.
"""
from django.core import checks


@checks.register(checks.Tags.compatibility)
def check_roles_overlay(app_configs, **kwargs):
    """E: the ROLES overlay must be well-formed and must not touch ``owner``."""
    from .conf import workspaces_settings

    errors = []
    overlay = workspaces_settings.ROLES or {}
    if not isinstance(overlay, dict):
        return [checks.Error(
            "STAPEL_WORKSPACES['ROLES'] must be a dict of {role: entry}.",
            id="stapel_workspaces.E001",
        )]
    for role, entry in overlay.items():
        if role == "owner":
            # Last-owner protection and "only an owner grants owner" are
            # hardcoded on this role; effective_roles() ignores the override.
            errors.append(checks.Error(
                "STAPEL_WORKSPACES['ROLES'] must not override or redefine "
                "the 'owner' role — it is system-protected.",
                id="stapel_workspaces.E002",
            ))
            continue
        if not isinstance(entry, dict):
            errors.append(checks.Error(
                f"STAPEL_WORKSPACES['ROLES'][{role!r}] must be a dict "
                "with 'rank' and 'capabilities'.",
                id="stapel_workspaces.E003",
            ))
            continue
        if not isinstance(entry.get("rank"), int):
            errors.append(checks.Error(
                f"STAPEL_WORKSPACES['ROLES'][{role!r}] requires an integer "
                "'rank' (ordering for role_at_least).",
                id="stapel_workspaces.E004",
            ))
        capabilities = entry.get("capabilities")
        if not isinstance(capabilities, list) or not all(
            isinstance(c, str) and c for c in capabilities
        ):
            errors.append(checks.Error(
                f"STAPEL_WORKSPACES['ROLES'][{role!r}] requires "
                "'capabilities' as a list of non-empty strings.",
                id="stapel_workspaces.E005",
            ))
        if len(role) > 32:
            errors.append(checks.Error(
                f"STAPEL_WORKSPACES['ROLES'] key {role!r} exceeds 32 "
                "characters (WorkspaceMember.role column width).",
                id="stapel_workspaces.E006",
            ))
    return errors


@checks.register(checks.Tags.compatibility)
def check_capability_levels(app_configs, **kwargs):
    """E: CAPABILITY_LEVELS values must be 'standard' or 'high'.

    A typo here would silently weaken (or block) the step-up gate on a
    sensitive capability — configuration the service cannot run with.
    """
    from .conf import workspaces_settings

    errors = []
    overlay = workspaces_settings.CAPABILITY_LEVELS or {}
    if not isinstance(overlay, dict):
        return [checks.Error(
            "STAPEL_WORKSPACES['CAPABILITY_LEVELS'] must be a dict of "
            "{capability: 'standard'|'high'}.",
            id="stapel_workspaces.E007",
        )]
    for capability, level in overlay.items():
        if level not in ("standard", "high"):
            errors.append(checks.Error(
                f"STAPEL_WORKSPACES['CAPABILITY_LEVELS'][{capability!r}] "
                f"must be 'standard' or 'high', got {level!r}.",
                id="stapel_workspaces.E008",
            ))
    return errors
