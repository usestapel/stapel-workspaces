"""Data Transfer Objects for workspaces API."""

from dataclasses import dataclass, field
from typing import Dict, List, Optional
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
class WorkspaceListResponse:
    """The caller's own workspace list.

    Attributes:
        workspaces: The caller's active memberships (empty for a guest — see is_guest).
        is_guest: True when the caller holds NO active mandate anywhere (permissions.is_guest) — the wire form of the mandate-model vardict's guest predicate (2026-08-03), so a frontend can render the "incomplete dashboard" without re-deriving it from workspaces == []. Example: false
        default_workspace_id: Which workspace a client should open when the person has expressed no choice — the instance's STAPEL_WORKSPACES["DEFAULT_WORKSPACE_ID"], echoed back ONLY when the caller actually holds an active membership in it, else "". Exists because clients that had to guess guessed badly: meettoday took `workspaces[0]` off a list ordered by last access, so a person in two spaces landed wherever they happened to have been last (measured 2026-08-06 as "the owner cannot see his own invitations" — they were in the OTHER space). A default, not a cage: an explicit choice still wins. Example: "a8bba7ae-5d2e-43e5-9911-0553e7df50b3"
        preferred_workspace_id: The person's OWN stated choice of home workspace (PUT me/preferred-workspace), echoed back ONLY while the membership carrying it is active, else "". This is the "explicit choice" default_workspace_id documents itself as yielding to, so a client resolves preferred first and the instance default only after it. Never inferred from behaviour — where somebody last clicked is last_accessed_at, which is telemetry, and reading it as a choice is what produced #239. Example: "a8bba7ae-5d2e-43e5-9911-0553e7df50b3"
        can_create_workspace: Whether THIS caller may found a new workspace on this instance (WORKSPACE_CREATE_POLICY, evaluated by services.can_create_workspace). The ANSWER, not the policy name: resolving "instance_owner" client-side would mean re-implementing the instance-owner lookup in every client, and a client that got it wrong would draw a button that 403s — or hide one that should be there. Rides the list because the workspace switcher is the surface that draws "+ New space", and it already fetches this. Example: true
    """

    workspaces: List[WorkspaceResponse] = field(default_factory=list)
    is_guest: bool = False
    default_workspace_id: str = ""
    preferred_workspace_id: str = ""
    can_create_workspace: bool = False


@dataclass
class PreferredWorkspaceRequest:
    """Which workspace the person is choosing as home.

    Attributes:
        workspace_id: UUID of a workspace the caller actively belongs to. Example: a8bba7ae-5d2e-43e5-9911-0553e7df50b3
    """

    workspace_id: UUID


@dataclass
class PreferredWorkspaceResponse:
    """The caller's stated home workspace after the write.

    Attributes:
        preferred_workspace_id: The workspace now recorded as the caller's choice, or "" after a DELETE cleared it. Example: a8bba7ae-5d2e-43e5-9911-0553e7df50b3
    """

    preferred_workspace_id: str = ""


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
        display_name: Best-effort display name — a live lookup in stapel-profiles when it is installed and has one, else the name typed at invite/provision time (never both, never invented when neither exists). Null when nobody has one yet. Example: Ada Lovelace
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
    display_name: Optional[str] = None


@dataclass
class MemberInviteRequest:
    """Invite payload.

    Attributes:
        emails: One or more emails to invite. Example: ["alice@example.com"]
        role: Role to grant on acceptance. Example: member
        display_name: Name hint for the invitee (this invite's "Name" field), applied to every email in this request. Optional — an invite without one behaves exactly as before. NOT written into stapel-profiles directly (that store is the invitee's own): held on the invitation, copied onto the member at accept time, and shown only until stapel-profiles has a real name for the person. Example: Ada Lovelace
    """

    emails: List[str]
    role: str = "member"
    display_name: Optional[str] = None


@dataclass
class MemberInviteResponse:  # noqa: R004
    invitations: List["InvitationResponse"] = field(default_factory=list)


@dataclass
class InvitationResponse:
    """One invitation row — the admin's "who has not accepted yet" table (#109).

    ``status`` is the derived label (``WorkspaceInvitation.status``), not a
    stored column: it is what the table renders and what the frontend
    routes the revoke/resend affordances on. The raw timestamps travel
    alongside it because "expires in 2 days" and "declined last Tuesday"
    are different rows on the same screen; the label alone cannot say
    when.

    The invite token is deliberately absent — it is a bearer secret that
    only ever leaves this service inside the invitation email.

    Attributes:
        id: Invitation UUID. Example: 0192f...
        workspace_id: Workspace UUID.
        email: Invited email address (normalized lowercase). Example: alice@example.com
        role: Role granted on acceptance. Example: member
        status: Derived state. One of pending / accepted / declined / revoked / expired. Example: pending
        expires_at: ISO 8601 expiry time. Example: 2026-07-31T10:00:00Z
        accepted_at: ISO 8601 acceptance time; null unless accepted.
        declined_at: ISO 8601 decline time (the invitee said no); null unless declined.
        revoked_at: ISO 8601 revocation time (the workspace withdrew it); null unless revoked.
        created_at: ISO 8601 creation time.
        invited_by_id: UUID of the admin who sent it; null when that account is gone. Example: 0192a...
        display_name: Name hint typed for this invitee at invite time, if any; null otherwise. Example: Ada Lovelace
        revoked_by_id: UUID of the admin who withdrew it; null unless revoked, or when that account is gone. Example: 0192a...
        last_sent_at: ISO 8601 time the invitation was last emailed; null if no letter was ever sent. Example: 2026-07-24T10:00:00Z
    """

    id: UUID
    workspace_id: UUID
    email: str
    role: str
    status: str
    expires_at: str
    accepted_at: Optional[str]
    declined_at: Optional[str]
    revoked_at: Optional[str]
    created_at: str
    invited_by_id: Optional[UUID] = None
    display_name: Optional[str] = None
    #: The actor behind ``revoked_at``. Paired with it deliberately: a
    #: screen that can say WHEN an invitation was withdrawn and not BY WHOM
    #: is the audit gap this field closes.
    revoked_by_id: Optional[UUID] = None
    #: When the invitee was last emailed about this invitation. The resend
    #: cooldown's clock, exposed so a client can disable its own resend
    #: affordance instead of discovering the 429 by pressing the button.
    last_sent_at: Optional[str] = None


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


@dataclass
class DisplayNameUpdateRequest:  # noqa: R004
    """Roster-side name correction (one field, both name-edit endpoints).

    Attributes:
        display_name: The name to show for this person. Held to stapel-profiles' display-name canon (validate_display_name) — this module never adds a second, differently-strict rule. Blank, whitespace-only, null and a missing key all mean the same thing: clear the name. Example: Ada Lovelace
    """

    display_name: Optional[str] = None


@dataclass
class DisplayNameResponse:
    """What the name is after the write — the stored value, not the request.

    Attributes:
        display_name: The stored name, trimmed and canon-checked. Empty string when the name was cleared. Example: Ada Lovelace
    """

    display_name: str


#: Allowed values of WorkspaceSecuritySettings.provisioned_user_policies —
#: mirrors auth's first_login_policies enum (spec §C2, #90). INDEPENDENT
#: checkboxes, not alternatives: an org may demand both.
PROVISIONED_USER_POLICIES = ("password_change", "mfa_enroll")

#: What an org gets when it has never touched the setting. Historical, and
#: deliberately non-empty: an org-provisioned account's password was chosen
#: by somebody other than its owner, so it must stop working at the first
#: login. See :attr:`WorkspaceSecuritySettings.policies_configured` for why
#: this default reaches provisioning but never invitation acceptance.
DEFAULT_PROVISIONED_USER_POLICIES = ("password_change",)


@dataclass
class WorkspaceSecuritySettings:
    """Typed shape of ``Workspace.settings["security"]`` (org-program spec §C3).

    Stored inside the free-form settings JSON (no schema migration); this
    dataclass is the canon of the known keys — extra keys in the block are
    preserved verbatim for client extension (the serializer seam validates
    only the known ones).

    Attributes:
        require_mfa: Whether membership requires a strong second factor. Turning it on sweeps current members via auth.mfa_status and suspends those without one (reason no_mfa); members losing their last strong factor later are suspended by the user.mfa_disabled consumer. Example: false
        provisioned_user_policies: First-login steps the org demands before an account it admits gets a session — INDEPENDENT checkboxes (#90), any subset of password_change / mfa_enroll. Applied to org-created accounts at provisioning and, when the org configured them explicitly, to invited accounts on acceptance. Example: ["password_change", "mfa_enroll"]
        policies_configured: Whether the org actually set the policies (as opposed to inheriting the default). Not a stored key — derived, and read-only on the wire. Example: false
    """

    require_mfa: bool = False
    provisioned_user_policies: List[str] = field(
        default_factory=lambda: list(DEFAULT_PROVISIONED_USER_POLICIES)
    )
    policies_configured: bool = False

    @classmethod
    def from_settings(cls, settings: Optional[dict]) -> "WorkspaceSecuritySettings":
        """Parse the ``security`` block of a workspace settings dict.

        Absent/malformed values fall back to the safe defaults — the same
        "absence means defaults" contract as auth.verification.policy.

        Two spellings of the policies are read, newest first:
        ``provisioned_user_policies`` (a list, #90) and the pre-0.13
        ``provisioned_user_policy`` (a single string), so a workspace row
        written by an older release keeps its meaning without a data
        migration. Unknown members are dropped rather than failing the
        parse: this runs on every provisioning and every accept, and a
        typo in one JSON blob must not take an org's invitations down.
        """
        block = (settings or {}).get("security") or {}
        if not isinstance(block, dict):
            block = {}
        require_mfa = block.get("require_mfa", False)

        raw = block.get("provisioned_user_policies")
        if raw is None:
            legacy = block.get("provisioned_user_policy")
            raw = [legacy] if isinstance(legacy, str) else None
        configured = raw is not None
        if not isinstance(raw, (list, tuple)):
            raw = list(DEFAULT_PROVISIONED_USER_POLICIES)
        wanted = set(raw)
        policies = [p for p in PROVISIONED_USER_POLICIES if p in wanted]

        return cls(
            require_mfa=bool(require_mfa) if isinstance(require_mfa, bool) else False,
            provisioned_user_policies=policies,
            policies_configured=configured,
        )

    def policies_for_invited_members(self) -> List[str]:
        """Policies to impose on someone who joins by ACCEPTING an invitation.

        Empty unless the org configured the setting itself. The default
        (``password_change``) exists for accounts the org *created*, where
        somebody other than the owner picked the password and it has to
        stop working; imposing it on a person who joined with their own,
        pre-existing account would force a password rotation nobody asked
        for on every new hire of every org that never opened the security
        screen. A silent default may harden an account the org minted; it
        may not reach into an account the org merely invited.
        """
        return list(self.provisioned_user_policies) if self.policies_configured else []


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
class MemberPasswordResetRequest:
    """Reset a member's password on the organization's order (#110).

    Attributes:
        password: New password chosen by the admin. Omitted: the server generates a crypto-strong one, returned once as generated_password. Example: null
        first_login_policies: First-login steps to demand of the member afterwards — any subset of password_change / mfa_enroll. Omitted: the workspace's own security policies, falling back to password_change. A password somebody else chose has to stop working at its first use, so an EMPTY list is a deliberate, auditable choice, not a shortcut. Example: ["password_change"]
        reason: Free-text note recorded on auth's audit row (e.g. the ticket the request came in on). Example: SUP-42
    """

    password: Optional[str] = None
    first_login_policies: Optional[List[str]] = None
    reason: Optional[str] = None


@dataclass
class MemberPasswordResetResponse:
    """Result of an administrative password reset (#110).

    Attributes:
        user_id: UUID of the member whose password was reset.
        generated_password: Server-generated password — returned exactly ONCE, only when the request omitted password. Hand it to the member out of band; it cannot be re-fetched. A credential: never log it. Example: null
        sessions_revoked: How many live sessions the reset ended. A reset that left them standing would not recover the account. Example: 2
        first_login_policies_applied: The steps now demanded of the member before their next session. Example: ["password_change"]
        notified: Whether the member was told their password was reset. False means the account has no contact channel — the admin is the only one who can tell them. Example: true
    """

    user_id: UUID
    sessions_revoked: int = 0
    generated_password: Optional[str] = None
    first_login_policies_applied: List[str] = field(default_factory=list)
    notified: bool = False


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
class RoleListResponse:
    """The effective role registry (builtin four + STAPEL_WORKSPACES overlay).

    Attributes:
        roles: One entry per effective role, descending rank.
        capability_levels: Step-up level per namespaced capability string, builtin ``members.provision`` / ``members.password.reset`` / ``workspace.security.manage`` plus the deployment's ``STAPEL_WORKSPACES["CAPABILITY_LEVELS"]`` overlay. A capability absent from this map is ``"standard"`` — the client-side default. Lets a frontend stop hardcoding its own copy of this registry. Example: {"members.provision": "high"}
    """

    roles: List[RoleResponse] = field(default_factory=list)
    capability_levels: Dict[str, str] = field(default_factory=dict)


@dataclass
class InstanceShapeResponse:
    """How THIS instance is deployed — what the client needs to know before
    deciding what to show a person with no workspace.

    The ``landing`` axis (``STREET_LANDING_MODE``) has existed since
    2026-08-03 but lived only in backend config, with nothing exposing it —
    a client had no way to tell a closed deployment from a public cloud one.

    This matters because the screen shown to someone who is no longer (or
    never was) a workspace member differs by deployment:

    * ``none`` — closed deployment: the person has no workspace and none is
      obtainable. An honest screen says "you're not a member, ask for an
      invite" and offers no path forward;
    * ``personal`` — public cloud: they have their own workspace, and the
      client can route them there.

    Offering "create a workspace" to someone the instance will never let
    create one is a dead end drawn as a button.

    Attributes:
        landing: What a "street" (no-invite) arrival lands on. Example: personal
        registration_open: Whether self-registration is available. Example: True
    """

    landing: str
    registration_open: bool
