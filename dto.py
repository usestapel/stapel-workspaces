"""Data Transfer Objects for workspaces API."""

from dataclasses import dataclass, field
from typing import List, Optional
from uuid import UUID


@dataclass
class WorkspaceResponse:
    """Workspace details.

    Attributes:
        id: Workspace UUID. Example: 0192f...
        name: Display name. Example: Acme Engineering
        slug: URL-safe identifier. Example: acme-eng
        type: Workspace category. Example: work
        owner_id: Owner user UUID. Example: 0192a...
        settings: Workspace settings JSON.
        storage_used_bytes: Bytes currently stored.
        storage_limit_bytes: Plan-determined cap.
        member_count: Number of members.
        my_role: Role of the requesting user. Example: owner
        created_at: ISO 8601 creation time. Example: 2026-05-20T10:00:00Z
        updated_at: ISO 8601 last update time. Example: 2026-05-20T10:00:00Z
    """

    id: UUID
    name: str
    slug: str
    type: str
    owner_id: UUID
    settings: dict
    storage_used_bytes: int
    storage_limit_bytes: int
    member_count: int
    my_role: Optional[str]
    created_at: str
    updated_at: str


@dataclass
class WorkspaceListResponse:
    workspaces: List[WorkspaceResponse] = field(default_factory=list)


@dataclass
class WorkspaceCreateRequest:
    """Create-workspace payload.

    Attributes:
        name: Display name. Example: Acme Engineering
        slug: URL-safe identifier (auto-generated when omitted). Example: acme-eng
        type: personal or work. Example: work
    """

    name: str
    slug: Optional[str] = None
    type: str = "work"


@dataclass
class WorkspaceUpdateRequest:
    name: Optional[str] = None
    slug: Optional[str] = None
    settings: Optional[dict] = None


@dataclass
class MemberResponse:
    """Workspace member.

    Attributes:
        id: Membership UUID. Example: 0192...
        workspace_id: Workspace UUID.
        user_id: User UUID.
        email: User email (best-effort, from JWT claim cache).
        role: owner / admin / member / viewer. Example: admin
        invited_at: ISO 8601 invite timestamp.
        accepted_at: ISO 8601 acceptance timestamp; null while pending.
        last_accessed_at: ISO 8601 last access; null if never accessed.
    """

    id: UUID
    workspace_id: UUID
    user_id: UUID
    email: Optional[str]
    role: str
    invited_at: str
    accepted_at: Optional[str]
    last_accessed_at: Optional[str]


@dataclass
class MemberListResponse:
    members: List[MemberResponse] = field(default_factory=list)


@dataclass
class MemberInviteRequest:
    """Invite payload.

    Attributes:
        emails: One or more emails to invite. Example: ["alice@example.com"]
        role: Role to grant on acceptance. Example: member
    """

    emails: List[str]
    role: str = "member"


@dataclass
class MemberInviteResponse:
    invitations: List["InvitationResponse"] = field(default_factory=list)


@dataclass
class InvitationResponse:
    id: UUID
    workspace_id: UUID
    email: str
    role: str
    expires_at: str
    accepted_at: Optional[str]
    revoked_at: Optional[str]
    created_at: str


@dataclass
class InvitationAcceptRequest:
    """Accept an invite.

    Attributes:
        token: Invite token from the email link.
    """

    token: str


@dataclass
class MemberUpdateRequest:
    role: str
