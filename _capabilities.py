"""stapel-workspaces capabilities.json emitter — thin shim over stapel_tools.capabilities."""
from pathlib import Path

from stapel_tools.capabilities import run_capabilities_cli


def _no_group(key: str) -> str:
    raise SystemExit(f"capabilities: stapel-workspaces has no axes, got key {key!r}")


def main(argv=None):
    from stapel_workspaces._codegen import _configure

    _configure()
    from stapel_workspaces.conf import DEFAULTS
    from stapel_workspaces.urls import GATE_REGISTRY

    # ROLES / CAPABILITY_LEVELS are merge-registry extension points (curated
    # in docs/capabilities.meta.json — same treatment as notifications'
    # TYPES); INVITATION_TTL_DAYS / PROVISION_USER_CREDITS are tuning. No
    # CTO-facing axes — is_axis matches nothing.
    return run_capabilities_cli(
        argv,
        repo=Path(__file__).resolve().parent,
        canonical_prefix="/workspaces/api/v1",
        defaults=DEFAULTS,
        registry=GATE_REGISTRY,
        is_axis=lambda k: False,
        axis_group=_no_group,
        prog="stapel-workspaces-capabilities",
    )


if __name__ == "__main__":
    raise SystemExit(main())
