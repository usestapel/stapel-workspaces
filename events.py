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
EVENT_WORKSPACE_MEMBER_PROVISIONED = "workspace.member_provisioned"
EVENT_WORKSPACE_MEMBER_SUSPENDED = "workspace.member_suspended"
EVENT_WORKSPACE_MEMBER_UNSUSPENDED = "workspace.member_unsuspended"
EVENT_WORKSPACE_INVITATION_REVOKED = "workspace.invitation_revoked"


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


@dataclass
class WorkspaceMemberProvisionedPayload:
    """Payload for the workspace.member_provisioned event.

    Emitted when an org-created (synthetic) account joins via
    POST members/provision (org-program spec §C1) — the audit/metering
    signal for the paid provisioning action. Never carries credential
    material (no password, generated or otherwise).

    Fields:
        workspace_id: UUID of the workspace.
        user_id: UUID of the provisioned user (created by auth.provision_user).
        role: Role granted to the new member.
        provisioned_by: UUID of the admin who performed the provisioning.
    """

    workspace_id: str
    user_id: str
    role: str
    provisioned_by: str


@dataclass
class WorkspaceMemberSuspendedPayload:
    """Payload for the workspace.member_suspended event.

    Suspension is not removal (org-program spec §C3): the membership row
    stays, but stops counting for every access check. Business services
    subscribe to revoke live access, exactly like member_removed.

    Fields:
        workspace_id: UUID of the workspace.
        user_id: UUID of the suspended member.
        role: Role the member holds (unchanged by suspension).
        reason: Suspension reason; canonical value "no_mfa" (require_mfa
            policy), vocabulary open for future reasons.
    """

    workspace_id: str
    user_id: str
    role: str
    reason: str


@dataclass
class WorkspaceMemberUnsuspendedPayload:
    """Payload for the workspace.member_unsuspended event.

    Fields:
        workspace_id: UUID of the workspace.
        user_id: UUID of the member whose suspension was lifted.
        role: Role the member holds.
        reason: The reason that was lifted (e.g. "no_mfa").
    """

    workspace_id: str
    user_id: str
    role: str
    reason: str


@dataclass
class WorkspaceInvitationRevokedPayload:
    """Payload for the workspace.invitation_revoked event (#109).

    The org withdrew a live invitation. Emitted through the transactional
    outbox in the same transaction as the ``revoked_at`` write, so it
    leaves iff the revocation commits — this is the audit answer to "who
    killed that invite", which the row itself cannot give (the model
    stores ``revoked_at`` but no ``revoked_by``).

    The invited **email is deliberately absent**: an invitation is
    addressed to a person who never consented to anything here, and an
    event fans out to every subscriber. The invitation UUID is enough for
    a subscriber that legitimately holds the row.

    Fields:
        workspace_id: UUID of the workspace.
        invitation_id: UUID of the revoked invitation.
        role: Role the invitation would have granted.
        revoked_by: UUID of the admin who withdrew it.
    """

    workspace_id: str
    invitation_id: str
    role: str
    revoked_by: str


EVENT_REGISTRY = {
    EVENT_WORKSPACE_PERSONAL_CREATED: WorkspacePersonalCreatedPayload,
    EVENT_WORKSPACE_MEMBER_REMOVED: WorkspaceMemberRemovedPayload,
    EVENT_WORKSPACE_MEMBER_ROLE_CHANGED: WorkspaceMemberRoleChangedPayload,
    EVENT_WORKSPACE_MEMBER_PROVISIONED: WorkspaceMemberProvisionedPayload,
    EVENT_WORKSPACE_MEMBER_SUSPENDED: WorkspaceMemberSuspendedPayload,
    EVENT_WORKSPACE_MEMBER_UNSUSPENDED: WorkspaceMemberUnsuspendedPayload,
    EVENT_WORKSPACE_INVITATION_REVOKED: WorkspaceInvitationRevokedPayload,
}
