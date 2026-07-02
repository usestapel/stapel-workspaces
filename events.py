"""Events published by stapel-workspaces.

Publishing goes through ``stapel_core.comm.emit`` (transactional outbox;
in-process in a monolith, bus in microservices) — see services.py. The
comm action name is ``workspace.personal.created``; its payload contract
lives in schemas/emits/workspace.personal.created.json.
"""
from dataclasses import dataclass

EVENT_WORKSPACE_PERSONAL_CREATED = "workspace.personal.created"

# On the bus transport the topic is the action name. The old Kafka topic
# ``stapel.workspaces.personal-created`` is retired; alias kept for any
# importer still referencing the old name.
TOPIC_WORKSPACE_PERSONAL_CREATED = EVENT_WORKSPACE_PERSONAL_CREATED


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
