"""Kafka events published by stapel-workspaces."""
from dataclasses import dataclass

TOPIC_WORKSPACE_PERSONAL_CREATED = "stapel.workspaces.personal-created"


@dataclass
class WorkspacePersonalCreatedPayload:
    """Payload for the workspace.personal.created event.

    Fields:
        user_id: UUID of the workspace owner.
        workspace_id: UUID of the created personal workspace.
    """

    user_id: str
    workspace_id: str


EVENT_REGISTRY = {
    TOPIC_WORKSPACE_PERSONAL_CREATED: WorkspacePersonalCreatedPayload,
}
