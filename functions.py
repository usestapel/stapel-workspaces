"""comm Function providers of the workspaces module.

Other modules check membership by name — no import of this app, no HTTP
client code (the transport is deployment configuration, see STAPEL_COMM):

    from stapel_core.comm import call

    result = call(
        "workspaces.check_membership",
        {"workspace_id": str(workspace_id), "user_id": str(user_id)},
    )
    # -> {"is_member": bool, "role": str | None}

The provider mirrors the internal HTTP endpoint
(:class:`stapel_workspaces.views.InternalMembershipView`): only *accepted*
memberships count.
"""

from stapel_core.comm import register_function

CHECK_MEMBERSHIP = "workspaces.check_membership"

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


def check_membership(payload: dict) -> dict:
    """Provider for ``workspaces.check_membership``.

    Payload: ``{"workspace_id": str, "user_id": str}``
    Returns: ``{"is_member": bool, "role": str | None}``
    """
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
        return {"is_member": False, "role": None}
    return {"is_member": True, "role": member.role}


def register() -> None:
    """Register this module's Function providers.

    Idempotent: re-registering the *same* handler object is a no-op, so
    AppConfig.ready() may run more than once without raising.
    """
    register_function(CHECK_MEMBERSHIP, check_membership, schema=CHECK_MEMBERSHIP_SCHEMA)
