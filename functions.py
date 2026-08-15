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
(:class:`stapel_workspaces.views.InternalMembershipView`): only *accepted*,
*non-suspended* memberships count (suspension closes access to the org
entirely — org-program spec §C3). ``capabilities`` carries the member role's *granted*
strings verbatim (wildcards like ``"*"`` / ``"members.*"`` included) — the
consumer-side matcher (``stapel_core.django.workspaces``) resolves them;
``workspaces.check_capability`` is the server-side resolution for a single
capability.
"""

from stapel_core.comm import register_function

CHECK_MEMBERSHIP = "workspaces.check_membership"
CHECK_CAPABILITY = "workspaces.check_capability"
#: The workspace-AGNOSTIC question: "does this user hold a mandate anywhere".
#: Both providers above are workspace-scoped, so neither could answer it, and
#: a sibling service that does not embed this app had no way to tell a guest
#: from a member. Name and payload are core's contract
#: (``stapel_core.django.mandate.MANDATE_FUNCTION``); this is its answering half.
CHECK_MANDATE = "workspaces.check_mandate"

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


# Kept in sync with schemas/functions/workspaces.check_mandate.json.
CHECK_MANDATE_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": CHECK_MANDATE,
    "type": "object",
    "required": ["user_id"],
    "properties": {
        "user_id": {"type": "string", "format": "uuid"},
    },
    "additionalProperties": False,
}


def check_mandate(payload: dict) -> dict:
    """Provider for ``workspaces.check_mandate``.

    Payload: ``{"user_id": str}``
    Returns: ``{"has_mandate": bool}``

    Same predicate the module's own guest surface uses
    (:func:`permissions.has_active_mandate`): accepted, non-suspended, in a
    workspace that is not soft-deleted. False here means the caller is a
    *guest* — a real account holding no mandate anywhere — which is a verdict.
    A caller that cannot reach this provider gets no verdict at all, and core
    turns that into 503 rather than into this False.
    """
    from .permissions import has_active_mandate_for_id

    return {"has_mandate": has_active_mandate_for_id(payload["user_id"])}


def check_membership(payload: dict) -> dict:
    """Provider for ``workspaces.check_membership``.

    Payload: ``{"workspace_id": str, "user_id": str}``
    Returns: ``{"is_member": bool, "role": str | None, "capabilities": [str]}``

    ``capabilities`` (additive, 0.6) lists the granted capability strings of
    the member's role — raw registry values, wildcards included.
    """
    from .capabilities import capabilities_for
    from .permissions import get_membership

    # Through the admission seam, not around it: this is another service
    # asking "may this person act here", and a workspace whose require_mfa
    # policy has not been proven for them must answer no here too (WORK-01).
    # The predicate is unchanged — get_membership selects active().
    member = get_membership(payload["workspace_id"], payload["user_id"])
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
    register_function(CHECK_MANDATE, check_mandate, schema=CHECK_MANDATE_SCHEMA)
