"""Events published by stapel-workspaces.

Publishing goes through ``stapel_core.comm.emit`` (transactional outbox;
in-process in a monolith, bus in microservices) — see services.py. The
comm action name is ``workspace.personal.created``; its payload contract
lives in schemas/emits/workspace.personal.created.json.
"""
from dataclasses import dataclass
from typing import List

EVENT_WORKSPACE_PERSONAL_CREATED = "workspace.personal.created"
EVENT_WORKSPACE_MEMBER_REMOVED = "workspace.member_removed"
EVENT_WORKSPACE_MEMBER_ROLE_CHANGED = "workspace.member_role_changed"


@dataclass
class WorkspacePersonalCreatedPayload:
    """Payload for the workspace.personal.created event.

    Fields:
        user_id: UUID of the workspace owner.
        workspace_id: UUID of the created personal workspace.
    """

    user_id: str
    workspace_id: str


@dataclass
class WorkspaceMemberRemovedPayload:
    """Payload for the workspace.member_removed event.

    Business services subscribe to revoke live access (org-program spec §A4 —
    e.g. a rooms service disconnecting a kicked user from an ongoing call).

    Fields:
        workspace_id: UUID of the workspace.
        user_id: UUID of the removed member.
        role: Role the member held at removal.
        removed_by: UUID of the actor who removed them.
    """

    workspace_id: str
    user_id: str
    role: str
    removed_by: str


@dataclass
class WorkspaceMemberRoleChangedPayload:
    """Payload for the workspace.member_role_changed event.

    Fields:
        workspace_id: UUID of the workspace.
        user_id: UUID of the member.
        old_role: Previous role.
        new_role: New role.
        capabilities: Granted capability strings of the NEW role, verbatim
            (registry values; wildcards like "*" included).
    """

    workspace_id: str
    user_id: str
    old_role: str
    new_role: str
    capabilities: List[str]


EVENT_REGISTRY = {
    EVENT_WORKSPACE_PERSONAL_CREATED: WorkspacePersonalCreatedPayload,
    EVENT_WORKSPACE_MEMBER_REMOVED: WorkspaceMemberRemovedPayload,
    EVENT_WORKSPACE_MEMBER_ROLE_CHANGED: WorkspaceMemberRoleChangedPayload,
}
