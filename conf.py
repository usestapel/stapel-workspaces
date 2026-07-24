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
        # credits debited per provisioned org user (0 = free)
        "PROVISION_USER_CREDITS": 0,
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
    # Credits debited per provisioned org user (0 = free).
    "PROVISION_USER_CREDITS": 0,
}

workspaces_settings = AppSettings(
    "STAPEL_WORKSPACES",
    defaults=DEFAULTS,
)

__all__ = ["workspaces_settings"]
