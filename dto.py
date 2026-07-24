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
        my_capabilities: Granted capability strings of the requesting user's role, verbatim from the registry (wildcards like * included). Example: ["workspace.view", "members.view"]
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
    my_capabilities: List[str] = field(default_factory=list)


@dataclass
class WorkspaceListResponse:  # noqa: R004
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
class WorkspaceUpdateRequest:  # noqa: R004
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
        provisioned: Whether this is an org-created (synthetic) member joined via members/provision. Example: false
        suspended_at: ISO 8601 suspension timestamp; null while active. Suspension is not removal — the role stays but access is closed. Example: null
        suspension_reason: Why the membership is suspended (canonical value no_mfa); null while active.
    """

    id: UUID
    workspace_id: UUID
    user_id: UUID
    email: Optional[str]
    role: str
    invited_at: str
    accepted_at: Optional[str]
    last_accessed_at: Optional[str]
    provisioned: bool = False
    suspended_at: Optional[str] = None
    suspension_reason: Optional[str] = None


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
class MemberInviteResponse:  # noqa: R004
    invitations: List["InvitationResponse"] = field(default_factory=list)


@dataclass
class InvitationResponse:  # noqa: R004
    id: UUID
    workspace_id: UUID
    email: str
    role: str
    expires_at: str
    accepted_at: Optional[str]
    revoked_at: Optional[str]
    created_at: str


@dataclass
class InvitationPreviewResponse:
    """Public (AllowAny) invitation preview — what the /invite/{token} page renders before any auth decision.

    Attributes:
        workspace_name: Display name of the inviting workspace. Example: Acme Engineering
        role: Role granted on acceptance. Example: member
        email_masked: Invited email, masked for the public page. Example: m***@e***.com
        status: Invitation state. One of pending / expired / revoked / accepted / declined. Example: pending
        email_registered: Whether an account already exists for the invited email — steers the frontend to login vs claim. Example: false
        expires_at: ISO 8601 expiry time. Example: 2026-07-31T10:00:00Z
    """

    workspace_name: str
    role: str
    email_masked: str
    status: str
    email_registered: bool
    expires_at: str


@dataclass
class InvitationClaimResponse:
    """Login-grant mint for an unregistered invitee (claim step).

    Attributes:
        grant_token: Single-use, short-lived login grant token — exchange it at auth's POST /grant/exchange/ for a session (creates the verified account). A credential: never log it.
    """

    grant_token: str


@dataclass
class InvitationAcceptRequest:
    """Accept an invite.

    Attributes:
        token: Invite token from the email link.
    """

    token: str


@dataclass
class MemberUpdateRequest:  # noqa: R004
    role: str


#: Allowed values of WorkspaceSecuritySettings.provisioned_user_policy —
#: mirrors auth.provision_user's first_login_policy enum (spec §C2).
PROVISIONED_USER_POLICIES = ("password_change", "mfa_enroll")


@dataclass
class WorkspaceSecuritySettings:
    """Typed shape of ``Workspace.settings["security"]`` (org-program spec §C3).

    Stored inside the free-form settings JSON (no schema migration); this
    dataclass is the canon of the known keys — extra keys in the block are
    preserved verbatim for client extension (the serializer seam validates
    only these two).

    Attributes:
        require_mfa: Whether membership requires a strong second factor. Turning it on sweeps current members via auth.mfa_status and suspends those without one (reason no_mfa); members losing their last strong factor later are suspended by the user.mfa_disabled consumer. Example: false
        provisioned_user_policy: First-login policy for org-created accounts — password_change (forced password change) or mfa_enroll (mandatory 2FA enrollment). Passed to auth.provision_user as first_login_policy. Example: password_change
    """

    require_mfa: bool = False
    provisioned_user_policy: str = "password_change"

    @classmethod
    def from_settings(cls, settings: Optional[dict]) -> "WorkspaceSecuritySettings":
        """Parse the ``security`` block of a workspace settings dict.

        Absent/malformed values fall back to the safe defaults — the same
        "absence means defaults" contract as auth.verification.policy.
        """
        block = (settings or {}).get("security") or {}
        if not isinstance(block, dict):
            block = {}
        require_mfa = block.get("require_mfa", False)
        policy = block.get("provisioned_user_policy", "password_change")
        return cls(
            require_mfa=bool(require_mfa) if isinstance(require_mfa, bool) else False,
            provisioned_user_policy=(
                policy if policy in PROVISIONED_USER_POLICIES else "password_change"
            ),
        )


@dataclass
class ProvisionMemberRequest:
    """Provision an org-created (synthetic) member (org-program spec §C1).

    Attributes:
        username_local: Local part of the login; the full username becomes "{workspace_slug}/{username_local}". Stock username alphabet, no slash. Example: jdoe
        password: Initial password chosen by the admin. Omitted: the server generates a crypto-strong one, returned once as generated_password. Example: null
        role: Role to grant (effective registry; owner is not provisionable). Example: member
        display_name: Display-name hint forwarded to auth/profiles. Example: Jane Doe
        email: Optional email anchor (stored UNVERIFIED by auth). When present, the provisioned-account email with the credentials is sent there. Normally omitted — synthetic accounts have no email. Example: null
    """

    username_local: str
    role: str = "member"
    password: Optional[str] = None
    display_name: Optional[str] = None
    email: Optional[str] = None


@dataclass
class ProvisionMemberResponse:
    """Result of provisioning an org member.

    Attributes:
        user_id: UUID of the created account.
        username: Full namespaced login. Example: acme-eng/jdoe
        role: Granted role. Example: member
        generated_password: Server-generated initial password — returned exactly ONCE, only when the request omitted password. Store it or hand it to the user now; it cannot be re-fetched. A credential: never log it.
    """

    user_id: UUID
    username: str
    role: str
    generated_password: Optional[str] = None


@dataclass
class RoleResponse:
    """One role of the effective registry (builtin + STAPEL_WORKSPACES overlay).

    Attributes:
        role: Role key. Example: admin
        rank: Ordering weight; higher = more powerful. Example: 300
        capabilities: Granted capability strings, verbatim (wildcards like * included). Example: ["workspace.view", "members.invite"]
        builtin: Whether the role key is one of the builtin four. Example: true
    """

    role: str
    rank: int
    capabilities: List[str]
    builtin: bool


@dataclass
class RoleListResponse:  # noqa: R004
    roles: List[RoleResponse] = field(default_factory=list)
