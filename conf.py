"""Settings namespace for stapel-workspaces.

Everything a host project previously had to fork is an override here::

    STAPEL_WORKSPACES = {
        # add product roles / override built-ins without touching the lib
        # (merge-registry, last-wins per role key — same canon as
        # STAPEL_NOTIFICATIONS["TYPES"]):
        "ROLES": {
            "secretary": {"rank": 250, "capabilities": [
                "workspace.view", "members.view", "members.remove",
                "meetings.spotlight", "meetings.kick",
            ]},
        },
        # raise the step-up level of individual capabilities
        # ("standard" | "high"), merged over capabilities.BUILTIN_CAPABILITY_LEVELS:
        "CAPABILITY_LEVELS": {"records.purge": "high"},
        # invitation link lifetime
        "INVITATION_TTL_DAYS": 7,
        # DRF throttle rate for the AllowAny invitation endpoints
        # (None disables throttling)
        "INVITATION_THROTTLE": "30/min",
        # seconds one invited address must wait between letters
        # (0 or None disables the cooldown)
        "INVITATION_RESEND_COOLDOWN_SECONDS": 600,
        # mint a fresh token on resend, killing the link already in the
        # invitee's mailbox (off by default — see the key's note below)
        "INVITATION_ROTATE_TOKEN_ON_RESEND": False,
        # credits debited per provisioned org user (0 = free)
        "PROVISION_USER_CREDITS": 0,
        # landing-mandate policy for an un-invited ("street") registration —
        # "personal" (default, back-compat) or "none" (closed-organization:
        # a fresh account is a guest until invited). See
        # services.resolve_landing_workspace.
        "STREET_LANDING_MODE": "personal",
    }

Resolution per key: STAPEL_WORKSPACES dict → env → default.
"""
from stapel_core.conf import AppSettings

#: AppSettings-shaped literal dict (capability-config.md §2): a top-level
#: DEFAULTS lets the capabilities.json emitter introspect axis keys/kinds
#: without re-parsing the AppSettings() call.
DEFAULTS = {
    # Role registry overlay, merged OVER capabilities.BUILTIN_ROLES
    # (last-wins per role key; an entry replaces the builtin entry whole).
    # {"<role>": {"rank": int, "capabilities": ["<domain>.<action>", ...]}}
    "ROLES": {},
    # Capability step-up levels overlay, merged over
    # capabilities.BUILTIN_CAPABILITY_LEVELS. {"<capability>": "standard"|"high"}
    "CAPABILITY_LEVELS": {},
    # Invitation link lifetime in days.
    "INVITATION_TTL_DAYS": 7,
    # DRF throttle rate (ScopedRateThrottle syntax, e.g. "30/min") for the
    # AllowAny invitation endpoints — GET invitations/<token> preview and
    # POST invitations/<token>/claim. The token is a bearer secret, but a
    # public endpoint still needs an enumeration backstop (spec §B2).
    # None disables throttling.
    "INVITATION_THROTTLE": "30/min",
    # Seconds that must pass before an invited address can be mailed about
    # its invitation again. 0 or None disables the cooldown.
    #
    # NOT a DRF rate like INVITATION_THROTTLE above, and the difference is
    # the point: a scoped DRF throttle counts requests per CALLER (user or
    # IP), which is the right shape for the AllowAny claim/preview endpoints
    # and the wrong shape here. The thing being protected on resend is not
    # this service's capacity — it is somebody else's INBOX. Ten admins
    # holding `members.invite`, or one admin on ten sessions, are ten
    # separate throttle buckets and one mailbox. So the clock lives on the
    # invited address (WorkspaceInvitation.last_sent_at, read across every
    # invitation for that address in the workspace), where the harm is.
    #
    # Before 0.23 there was neither a cooldown nor a record of the previous
    # send: POST invitations/<id>/resend could be driven in a loop, and each
    # pass mailed the address again (and, with rotation still on, churned
    # the credential). The default is 10 minutes, matching the cooldown the
    # room-invite path in the meettoday product already carries — one number
    # for "how often may we mail the same person about the same thing".
    "INVITATION_RESEND_COOLDOWN_SECONDS": 600,
    # Whether a resend mints a NEW token and kills the old link.
    #
    # Default False, reversing the pre-0.23 hardcoded behaviour, and the
    # reason belongs here rather than in a commit message:
    #
    # * A resend goes to the SAME address as the original letter. Rotation
    #   therefore does not narrow the credential's exposure at all — it
    #   moves it from one letter in that mailbox to another letter in the
    #   same mailbox. The old argument ("a resend means nobody knows where
    #   the first copy went") does not survive contact with the fact that
    #   the second copy goes exactly where the first one did.
    # * It has a real cost the other way round. The overwhelmingly common
    #   resend is "they say it never arrived" — and then the invitee finds
    #   the first letter (spam folder, threaded client, a colleague's
    #   forward) and clicks it. With rotation that link answers
    #   `error.404.invitation_not_found`, which reads to the person as "the
    #   invitation was cancelled" rather than "you clicked the older of two
    #   identical letters".
    # * When a token IS believed to have leaked, the answer is not a resend:
    #   it is revoke + invite again, which already exists, mints a genuinely
    #   new invitation and leaves a `revoked_by` audit trail.
    #
    # Deployments that want the old behaviour (a strict org where every
    # resend must invalidate its predecessor) set this True; nothing else
    # changes, and the cooldown above bounds how often it can happen either
    # way. The invite token never appears in any API response, so this key
    # changes no response body — only whether a link already in somebody's
    # mailbox keeps working.
    "INVITATION_ROTATE_TOKEN_ON_RESEND": False,
    # Credits debited per provisioned org user (0 = free).
    "PROVISION_USER_CREDITS": 0,
    # Mandate axis for an un-invited ("street") registration — the policy
    # `resolve_landing_workspace(user, origin=...)` reads for every origin
    # OTHER than "invited" (org-program #85, mandate-model vardict 2026-08-03).
    # * "personal" (default — back-compat): the pre-#85 behavior, unchanged
    #   for every existing deployment that does not set this key. A fresh
    #   account gets its own Personal workspace and is its OWNER — this is
    #   the OSS/demo-cloud shape (self-serve signup, no invitation needed).
    # * "none": a fresh account lands with NO workspace at all — a
    #   registered account is a "guest" (see permissions.is_guest) until an
    #   existing organization invites it. This is the closed-organization
    #   shape; a host switches to it deliberately, it is never silently
    #   turned on by a version bump.
    "STREET_LANDING_MODE": "personal",
    # The instance's DEFAULT workspace id (a uuid string), or "" for none.
    #
    # Without it a client has no way to know which workspace to open, and the
    # ones that guessed guessed badly: meettoday's frontend took
    # `workspaces[0]` — literally the first row of a list ordered by
    # `-last_accessed_at` — so a person who belonged to two spaces landed in
    # whichever they had touched last. Measured 2026-08-06: the owner's four
    # pending invitations sat in the org space while the screen showed his
    # PERSONAL one, and read as "the owner cannot see his own invitations".
    #
    # This key names the answer once, on the server, so every client resolves
    # it the same way. It is a DEFAULT, not a cage: a person still switches
    # spaces, and their explicit choice wins over it.
    #
    # Exposed to clients through the workspace list response, and ONLY when
    # the caller actually holds an active membership in it — pointing a client
    # at a space it cannot open would trade one wrong screen for another.
    "DEFAULT_WORKSPACE_ID": "",
    # WHO MAY CREATE A WORKSPACE. "" (default) means "derive it from
    # STREET_LANDING_MODE", which is where the question actually comes from:
    #
    # * "open" — anyone with an account. The public-cloud shape, and what
    #   `STREET_LANDING_MODE="personal"` implies: an instance that mints a
    #   personal workspace for every signup has already answered "yes";
    # * "instance_owner" — only the OWNER of the instance's default workspace
    #   (see `instance_owner_ids`). The private-cloud shape, and what any
    #   non-personal landing mode implies: on an instance where entry is by
    #   invitation, a member who could mint their own org would step outside
    #   the org they were invited into. Everyone else still SWITCHES between
    #   the spaces they are in — this restricts creation, not choice;
    # * "closed" — nobody, through the API. Spaces are provisioned by an
    #   operator (`manage.py provision_space`) and by nothing else.
    #
    # Derived rather than defaulted to a literal because the two axes answer
    # the same product question, and a deployment that switched to a closed
    # landing mode and kept an "open" creation policy would have a private
    # cloud whose members can each spin up their own org — a gap nobody would
    # notice until it was populated. An explicit value always wins.
    "WORKSPACE_CREATE_POLICY": "",
    # WHERE THE MEMBERSHIP JOURNAL GOES — callable(stream, payload, *,
    # project, container), the same sink contract the privilege gateway's
    # STAPEL_GATEWAY["AUDIT_SINK"] uses, so one custom sink (a SIEM
    # shipper, a syslog writer) can serve both seams. The default appends
    # to stapel_core.eventstore and flushes; see audit.eventstore_sink for
    # why the flush is part of the contract here. A deployment that swaps
    # the sink away from the event store takes over serving the history
    # too: GET <workspace_id>/audit reads the event store and only the
    # event store.
    "AUDIT_SINK": "stapel_workspaces.audit.eventstore_sink",
    # The event-store stream membership history is written to and read
    # from. One stream per journal, not per workspace: the workspace is a
    # payload field (`workspace_id`) the read side filters on, the way
    # every other stream consumer slices. Retention/rollup/backends key on
    # this name via STAPEL_EVENTSTORE (RETENTION, ROUTES).
    "AUDIT_STREAM": "workspace.audit",
}

#: The three answers `WORKSPACE_CREATE_POLICY` may take, plus "" for derived.
CREATE_POLICY_OPEN = "open"
CREATE_POLICY_INSTANCE_OWNER = "instance_owner"
CREATE_POLICY_CLOSED = "closed"
CREATE_POLICIES = frozenset(
    {CREATE_POLICY_OPEN, CREATE_POLICY_INSTANCE_OWNER, CREATE_POLICY_CLOSED}
)

workspaces_settings = AppSettings(
    "STAPEL_WORKSPACES",
    defaults=DEFAULTS,
    import_strings=("AUDIT_SINK",),
    # The sink decides what code runs on every membership transition — a
    # stray same-named env var must never swap it silently (the gateway
    # applies the same rule to its AUDIT_SINK).
    no_env=("AUDIT_SINK",),
)

#: Values an environment variable may spell "yes" with. AppSettings resolves
#: env vars as raw strings (it has no per-key type), and ``bool("false")`` is
#: True — a deployment that set ``INVITATION_ROTATE_TOKEN_ON_RESEND=false``
#: would get rotation. Anything not in this set is False.
_TRUTHY = {"1", "true", "yes", "on"}


def resend_cooldown_seconds() -> int:
    """``INVITATION_RESEND_COOLDOWN_SECONDS`` as an int; 0 when disabled.

    Accepts the int a settings dict carries and the string an environment
    variable carries (AppSettings does no coercion — see
    :data:`_TRUTHY`). A value that is neither is 0 here and an E009 from
    ``checks.check_invitation_resend_cooldown``, which is the loud half:
    silently defaulting a broken rate limit to "off" is how a deployment
    ends up believing it has one.
    """
    value = workspaces_settings.INVITATION_RESEND_COOLDOWN_SECONDS
    if isinstance(value, bool) or value is None:
        return 0
    if isinstance(value, int):
        return max(0, value)
    try:
        return max(0, int(str(value).strip()))
    except (TypeError, ValueError):
        return 0


def rotate_token_on_resend() -> bool:
    """``INVITATION_ROTATE_TOKEN_ON_RESEND`` as a bool (see that key)."""
    value = workspaces_settings.INVITATION_ROTATE_TOKEN_ON_RESEND
    if isinstance(value, str):
        return value.strip().lower() in _TRUTHY
    return bool(value)


def workspace_create_policy() -> str:
    """The effective ``WORKSPACE_CREATE_POLICY`` — one of :data:`CREATE_POLICIES`.

    Empty (the default) derives it from ``STREET_LANDING_MODE``: ``personal``
    is a public cloud and answers ``open``, anything else is a closed instance
    and answers ``instance_owner``. See the key's own comment for why the
    derivation exists rather than a literal default.

    An unrecognized value resolves to ``instance_owner`` — the restrictive
    answer — and is an E010 from ``checks.check_workspace_create_policy``.
    Degrading a misspelled policy to "open" would silently hand every member
    of a private cloud the ability to found their own org, which is exactly
    the failure this key exists to prevent; the loud half is the check.
    """
    raw = str(workspaces_settings.WORKSPACE_CREATE_POLICY or "").strip().lower()
    if not raw:
        landing = str(workspaces_settings.STREET_LANDING_MODE or "personal").strip()
        return CREATE_POLICY_OPEN if landing == "personal" else CREATE_POLICY_INSTANCE_OWNER
    if raw not in CREATE_POLICIES:
        return CREATE_POLICY_INSTANCE_OWNER
    return raw


__all__ = [
    "workspaces_settings",
    "resend_cooldown_seconds",
    "rotate_token_on_resend",
    "workspace_create_policy",
    "CREATE_POLICY_OPEN",
    "CREATE_POLICY_INSTANCE_OWNER",
    "CREATE_POLICY_CLOSED",
    "CREATE_POLICIES",
]
