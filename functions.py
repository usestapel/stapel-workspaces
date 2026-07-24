"""comm Function providers of the workspaces module.

Other modules check membership by name — no import of this app, no HTTP
client code (the transport is deployment configuration, see STAPEL_COMM):

    from stapel_core.comm import call

    result = call(
        "workspaces.check_membership",
        {"workspace_id": str(workspace_id), "user_id": str(user_id)},
    )
    # -> {"is_member": bool, "role": str | None, "capabilities": [str]}

    result = call(
        "workspaces.check_capability",
        {"workspace_id": ..., "user_id": ..., "capability": "meetings.kick"},
    )
    # -> {"allowed": bool, "role": str | None}

The providers mirror the internal HTTP endpoint
(:class:`stapel_workspaces.views.InternalMembershipView`): only *accepted*
memberships count. ``capabilities`` carries the member role's *granted*
strings verbatim (wildcards like ``"*"`` / ``"members.*"`` included) — the
consumer-side matcher (``stapel_core.django.workspaces``) resolves them;
``workspaces.check_capability`` is the server-side resolution for a single
capability.
"""

from stapel_core.comm import register_function

CHECK_MEMBERSHIP = "workspaces.check_membership"
CHECK_CAPABILITY = "workspaces.check_capability"

# Kept in sync with schemas/functions/workspaces.check_membership.json
# (the schemas/ autoloader registers the file too; passing it here makes
# validation work even without the autoloader).
CHECK_MEMBERSHIP_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": CHECK_MEMBERSHIP,
    "type": "object",
    "required": ["workspace_id", "user_id"],
    "properties": {
        "workspace_id": {"type": "string", "format": "uuid"},
        "user_id": {"type": "string", "format": "uuid"},
    },
    "additionalProperties": False,
}

# Kept in sync with schemas/functions/workspaces.check_capability.json.
CHECK_CAPABILITY_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": CHECK_CAPABILITY,
    "type": "object",
    "required": ["workspace_id", "user_id", "capability"],
    "properties": {
        "workspace_id": {"type": "string", "format": "uuid"},
        "user_id": {"type": "string", "format": "uuid"},
        "capability": {"type": "string", "minLength": 1},
    },
    "additionalProperties": False,
}


def check_membership(payload: dict) -> dict:
    """Provider for ``workspaces.check_membership``.

    Payload: ``{"workspace_id": str, "user_id": str}``
    Returns: ``{"is_member": bool, "role": str | None, "capabilities": [str]}``

    ``capabilities`` (additive, 0.6) lists the granted capability strings of
    the member's role — raw registry values, wildcards included.
    """
    from .capabilities import capabilities_for
    from .models import WorkspaceMember

    member = (
        WorkspaceMember.objects.filter(
            workspace_id=payload["workspace_id"],
            user_id=payload["user_id"],
            accepted_at__isnull=False,
        )
        .only("role")
        .first()
    )
    if member is None:
        return {"is_member": False, "role": None, "capabilities": []}
    return {
        "is_member": True,
        "role": member.role,
        "capabilities": capabilities_for(member.role),
    }


def check_capability(payload: dict) -> dict:
    """Provider for ``workspaces.check_capability``.

    Payload: ``{"workspace_id": str, "user_id": str, "capability": str}``
    Returns: ``{"allowed": bool, "role": str | None}``
    """
    from .permissions import get_membership, require_capability

    member = require_capability(
        payload["workspace_id"], payload["user_id"], payload["capability"]
    )
    if member is not None:
        return {"allowed": True, "role": member.role}
    membership = get_membership(payload["workspace_id"], payload["user_id"])
    return {"allowed": False, "role": membership.role if membership else None}


def register() -> None:
    """Register this module's Function providers.

    Idempotent: re-registering the *same* handler object is a no-op, so
    AppConfig.ready() may run more than once without raising.
    """
    register_function(CHECK_MEMBERSHIP, check_membership, schema=CHECK_MEMBERSHIP_SCHEMA)
    register_function(CHECK_CAPABILITY, check_capability, schema=CHECK_CAPABILITY_SCHEMA)
