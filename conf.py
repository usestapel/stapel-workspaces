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
}

workspaces_settings = AppSettings(
    "STAPEL_WORKSPACES",
    defaults=DEFAULTS,
)

__all__ = ["workspaces_settings"]
