"""stapel-workspaces capabilities.json emitter — thin shim over stapel_tools.capabilities."""
from pathlib import Path

from stapel_tools.capabilities import run_capabilities_cli


def _no_group(key: str) -> str:
    raise SystemExit(f"capabilities: stapel-workspaces has no axes, got key {key!r}")


def main(argv=None):
    from stapel_workspaces._codegen import _configure

    _configure()
    from stapel_workspaces.urls import GATE_REGISTRY

    # This module has NO settings namespace (no conf.py, capability-config.md
    # §2: "модули без ручек получают валидный манифест с axes: []") — defaults
    # is empty and is_axis matches nothing; provides/requires/extension_points
    # still come from the curated docs/capabilities.meta.json.
    return run_capabilities_cli(
        argv,
        repo=Path(__file__).resolve().parent,
        canonical_prefix="/workspaces/api/v1",
        defaults={},
        registry=GATE_REGISTRY,
        is_axis=lambda k: False,
        axis_group=_no_group,
        prog="stapel-workspaces-capabilities",
    )


if __name__ == "__main__":
    raise SystemExit(main())
