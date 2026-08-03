"""stapel-workspaces capabilities.json emitter — thin shim over stapel_tools.capabilities."""
from pathlib import Path

from stapel_tools.capabilities import axis_group_rules, run_capabilities_cli


def main(argv=None):
    from stapel_workspaces._codegen import _configure

    _configure()
    from stapel_workspaces.conf import DEFAULTS
    from stapel_workspaces.urls import GATE_REGISTRY

    # ROLES / CAPABILITY_LEVELS are merge-registry extension points (curated
    # in docs/capabilities.meta.json — same treatment as notifications'
    # TYPES); INVITATION_TTL_DAYS / PROVISION_USER_CREDITS are tuning. One
    # CTO-facing axis — STREET_LANDING_MODE (mandate-model vardict 2026-08-03,
    # org-program #85): personal|none, whether an un-invited registration
    # gets its own workspace or lands as a guest. Behavioral, not gating (no
    # URL-factory flag unmounts) — same treatment as calendar's VISIBILITY.
    return run_capabilities_cli(
        argv,
        repo=Path(__file__).resolve().parent,
        canonical_prefix="/workspaces/api/v1",
        defaults=DEFAULTS,
        registry=GATE_REGISTRY,
        is_axis=lambda k: k == "STREET_LANDING_MODE",
        axis_group=axis_group_rules(
            exact={"STREET_LANDING_MODE": "workspaces.landing"}
        ),
        prog="stapel-workspaces-capabilities",
    )


if __name__ == "__main__":
    raise SystemExit(main())
