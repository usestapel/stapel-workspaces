"""Events published by stapel-workspaces.

Publishing goes through ``stapel_core.comm.emit`` (transactional outbox;
in-process in a monolith, bus in microservices) — see services.py. The
comm action name is ``workspace.personal.created``; its payload contract
lives in schemas/emits/workspace.personal.created.json.
"""
from dataclasses import dataclass

EVENT_WORKSPACE_PERSONAL_CREATED = "workspace.personal.created"

# Legacy Kafka topic name kept for consumers still subscribed by topic.
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
    EVENT_WORKSPACE_PERSONAL_CREATED: WorkspacePersonalCreatedPayload,
    TOPIC_WORKSPACE_PERSONAL_CREATED: WorkspacePersonalCreatedPayload,
}
