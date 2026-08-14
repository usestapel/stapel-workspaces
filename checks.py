"""Django system checks for stapel-workspaces configuration.

Policy (docs/library-standard.md §3.7): E-level for configuration the
service cannot run with. The role registry drives authorization, so a
malformed ``STAPEL_WORKSPACES["ROLES"]`` overlay must block deploys rather
than silently deny (or worse, grant) capabilities at runtime.
"""
from django.core import checks


def _function_unreachable_reason(name: str) -> str | None:
    """Why comm Function *name* cannot be called here, or None if it can.

    "Is this seam wired" is a question about the transport this deployment
    runs, and each transport addresses a function differently
    (``stapel_core.comm``):

    * ``inprocess`` — ``call()`` looks the name up in the process-local
      registry, so a provider must be registered here;
    * ``http`` — ``call()`` resolves a longest-prefix ``FUNCTION_ROUTES``
      entry and ignores the registry entirely, so only a matching route
      makes the function reachable — even in a process that also provides
      it;
    * ``nats`` — the subject IS the function name and no route table
      exists (``comm/nats.py``); a deployment that serves the function with
      ``manage.py serve_functions`` is wired, and nothing here can (or
      should) prove the provider is up. That is what the runtime 503 is for;
    * a dotted path — a custom transport does its own addressing.

    Anything else is a transport ``call()`` cannot dispatch at all: it
    raises ``FunctionRouteNotConfigured`` on every call, so the seam is as
    unreachable as an unwired one.

    Reading ``FUNCTION_ROUTES`` regardless of transport — which E011 and
    W001 both did — reported a correctly wired NATS fleet as unwired.
    """
    from stapel_core.comm.config import comm_setting
    from stapel_core.comm.exceptions import (
        FunctionNotRegistered,
        FunctionRouteNotConfigured,
    )
    from stapel_core.comm.functions import _route_for
    from stapel_core.comm.registry import function_registry

    transport = str(comm_setting("FUNCTION_TRANSPORT", "inprocess") or "")
    if transport == "inprocess":
        try:
            function_registry.get(name)
        except FunctionNotRegistered:
            return (
                f"the transport is 'inprocess' and no provider for {name} is "
                "registered in this process"
            )
        return None
    if transport == "http":
        try:
            _route_for(name)
        except FunctionRouteNotConfigured:
            return (
                f"the transport is 'http' and no STAPEL_COMM['FUNCTION_ROUTES'] "
                f"prefix matches {name}"
            )
        return None
    if transport == "nats" or "." in transport:
        return None
    return (
        f"STAPEL_COMM['FUNCTION_TRANSPORT'] is {transport!r}, which is not a "
        "transport comm can dispatch on ('inprocess', 'nats', 'http', or a "
        "dotted path to a transport callable)"
    )


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


@checks.register(checks.Tags.compatibility)
def check_invitation_resend_cooldown(app_configs, **kwargs):
    """E: the resend cooldown must be a number of seconds, or nothing.

    A rate limit that a typo turns off is worse than no rate limit, because
    the deployment believes it has one. ``"10m"`` (the DRF-rate shape the
    neighbouring INVITATION_THROTTLE uses) or ``True`` would otherwise
    sail through, and the first evidence would be a mailbox.
    """
    from .conf import workspaces_settings

    value = workspaces_settings.INVITATION_RESEND_COOLDOWN_SECONDS
    if value is None:
        return []
    ok = False
    if not isinstance(value, bool):
        if isinstance(value, int):
            ok = value >= 0
        elif isinstance(value, str):
            # An environment variable arrives as a string; AppSettings does
            # no coercion. "300" is a legitimate way to configure this.
            ok = value.strip().isdigit()
    if not ok:
        return [checks.Error(
            "STAPEL_WORKSPACES['INVITATION_RESEND_COOLDOWN_SECONDS'] must be "
            f"a non-negative number of SECONDS (or None/0 to disable), got "
            f"{value!r}.",
            hint="This key is a duration, not a DRF rate string like "
                 "INVITATION_THROTTLE: the window belongs to the invited "
                 "address, not to the calling admin.",
            id="stapel_workspaces.E009",
        )]
    return []


@checks.register(checks.Tags.compatibility)
def check_profiles_name_write_wired(app_configs, **kwargs):
    """W: the roster can edit a name, but nothing can perform the write.

    ``PATCH <ws>/members/<id>/name`` writes stapel-profiles'
    ``Profile.display_name`` through ``profiles.set_display_name`` — a comm
    Function that module publishes from 0.10. If this deployment has neither
    a provider (profiles not in this process) nor a route to one, that
    endpoint answers ``error.503.profiles_not_configured`` on every request,
    forever, and only an operator can change that.

    Modelled on stapel-core's CDN route check
    (``stapel_core.django.cdn.checks.check_cdn_module_wired``, E002), which
    is the fleet's existing answer to "a comm route this code needs is not
    configured". W and not E on purpose: this is one endpoint of many, and
    env-address-class v2 §2 says a dependency that serves part of a
    process's surface degrades LOUDLY rather than blocking the start of
    everything else. Loud at deploy time beats loud at the first user.
    """
    from .services import SET_DISPLAY_NAME

    reason = _function_unreachable_reason(SET_DISPLAY_NAME)
    if reason is not None:
        return [checks.Warning(
            f"{SET_DISPLAY_NAME} is not reachable in this deployment ({reason}) "
            "— PATCH <workspace>/members/<user_id>/name cannot write the "
            "canonical display name and will answer "
            "error.503.profiles_not_configured on every request.",
            hint="Add stapel_profiles (>= 0.10) to INSTALLED_APPS in this "
                 "process, or add a STAPEL_COMM['FUNCTION_ROUTES'] entry for "
                 "'profiles.' pointing at the service that runs it.",
            id="stapel_workspaces.W001",
        )]
    return []


@checks.register(checks.Tags.compatibility)
def check_workspace_create_policy(app_configs, **kwargs):
    """E: an unrecognized ``WORKSPACE_CREATE_POLICY``; W: one nobody can satisfy.

    Two different failures, deliberately at two different levels.

    The E is a misspelling. ``workspace_create_policy()`` resolves anything
    unrecognized to the RESTRICTIVE answer, so the deployment does not quietly
    become an open cloud — but a private instance whose owner typed
    ``"instance-owner"`` and got a policy they did not name is exactly the kind
    of silent substitution that only surfaces the day someone founds an org.

    The W is a policy with nobody to satisfy it: ``instance_owner`` with no
    ``DEFAULT_WORKSPACE_ID`` means the API refuses EVERY creation, from
    everybody, forever. That may be intentional (``closed`` says it plainly),
    but reached this way it is almost always an unfinished deployment — and a
    warning at boot beats an owner discovering it when their button 403s.
    Warning rather than Error because the instance otherwise runs perfectly:
    creation is one endpoint, and env-address-class v2 §2 says a partial
    surface degrades loudly rather than blocking the start of everything else.
    """
    from .conf import (
        CREATE_POLICIES,
        CREATE_POLICY_INSTANCE_OWNER,
        workspace_create_policy,
        workspaces_settings,
    )

    raw = str(workspaces_settings.WORKSPACE_CREATE_POLICY or "").strip()
    if raw and raw.lower() not in CREATE_POLICIES:
        return [checks.Error(
            f"STAPEL_WORKSPACES['WORKSPACE_CREATE_POLICY'] is {raw!r}, which is "
            f"not one of {sorted(CREATE_POLICIES)}.",
            hint="Leave it empty to derive the policy from "
                 "STREET_LANDING_MODE (personal -> open, otherwise -> "
                 "instance_owner). Until this is fixed the effective policy "
                 "is 'instance_owner' — the restrictive reading.",
            id="stapel_workspaces.E010",
        )]

    if (
        workspace_create_policy() == CREATE_POLICY_INSTANCE_OWNER
        and not str(workspaces_settings.DEFAULT_WORKSPACE_ID or "").strip()
    ):
        return [checks.Warning(
            "The effective workspace-creation policy is 'instance_owner', but "
            "STAPEL_WORKSPACES['DEFAULT_WORKSPACE_ID'] is unset — the instance "
            "owner is defined as the OWNER of that workspace, so nobody can "
            "create a workspace through the API.",
            hint="Set DEFAULT_WORKSPACE_ID to the instance's own workspace, or "
                 "state the intent directly with "
                 "WORKSPACE_CREATE_POLICY='closed' (provisioning by "
                 "`manage.py provision_space` only) or 'open'.",
            id="stapel_workspaces.W002",
        )]
    return []


@checks.register(checks.Tags.compatibility)
def check_billing_seam_wired(app_configs, **kwargs):
    """E: plan ceilings are enforced but nothing can answer them.

    ``check_entitlement`` fails closed, so a deployment that neither ships
    billing nor routes ``billing.`` to it answers 503 to every org
    creation, invitation and provisioning — forever, and only once a user
    presses the button. This is the boot-time half of that: the operator
    either wires the seam or states that the instance sells nothing
    (``ALLOW_UNBILLED``), and finds out at deploy either way.

    What "wired" means is whatever the deployment's FUNCTION_TRANSPORT
    makes it mean — see :func:`_function_unreachable_reason`. Every
    topology is real: billing in this process (monolith), billing behind an
    http route, billing behind a NATS subject with no route table at all.
    None of them is a liveness probe; a wired-but-down billing is exactly
    the case the 503 exists for.
    """
    from .conf import allow_unbilled
    from .entitlements import CHECK_ENTITLEMENT

    if allow_unbilled():
        return []
    try:
        reason = _function_unreachable_reason(CHECK_ENTITLEMENT)
    except ImportError:  # pragma: no cover - comm ships with stapel-core
        return []
    if reason is None:
        return []
    return [checks.Error(
        f"Plan ceilings fail closed, but {CHECK_ENTITLEMENT} is not reachable "
        f"in this deployment: {reason}. Creating an organization, inviting a "
        "member and provisioning a user will all answer 503.",
        hint="Wire billing for the transport this deployment runs — install "
             "stapel_billing in this process (inprocess), route 'billing.' to "
             "the service that owns it (http), or run that service's "
             "`manage.py serve_functions` (nats) — or declare that this "
             "instance sells nothing with "
             "STAPEL_WORKSPACES['ALLOW_UNBILLED'] = True.",
        id="stapel_workspaces.E011",
    )]
