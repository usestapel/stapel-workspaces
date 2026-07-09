"""stapel-workspaces contract-emission harness (contract-pipeline.md §2-3).

Emits the module's own contract triad into ``docs/`` from a single-module
``{workspaces + core}`` Django instance mounted at the canonical
``workspaces/api/`` prefix:

  docs/schema.json   drf-spectacular OpenAPI, this module only, canonical prefix
  docs/flows.json    generate_flow_docs machine artifact, canonical-prefix paths
                      (empty array — this module has no ``@flow_step`` yet)
  docs/errors.json   generate_error_keys registry (already the etalon)

Copies stapel-auth's ``_codegen.py`` (the ETALON), adapted: workspaces' conftest
does not bootstrap an eager Celery app (no ``shared_task`` in this module) and
has no flat-layout subpackage that would shadow an installed package (auth's
``openid/`` vs python3-openid), so both guards are omitted here.

Usage:
    python -m stapel_workspaces._codegen --out docs        # `make contract`
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _configure() -> None:
    """Configure + boot the single-module Django instance for emission."""
    from django.conf import settings

    if not settings.configured:
        from stapel_workspaces._codegen_settings import settings_kwargs

        settings.configure(
            **settings_kwargs(
                root_urlconf="stapel_workspaces.codegen_urls", contract=True
            )
        )

    import django

    django.setup()

    # drf-spectacular froze its settings singleton at import time (before this
    # harness ran configure()), so it is on drf defaults — the same state the
    # monolith emits under. The one knob to force is SCHEMA_PATH_PREFIX: left
    # None, drf derives the operationId prefix from the common path of all
    # endpoints — "/" across the multi-module monolith (operationIds keep the
    # mount segment, workspaces_api_*), but "/workspaces/api" in a single-module
    # harness (which would strip it to bare anonymous names). Pin it to the
    # monolith's common prefix so the operationIds are byte-identical;
    # SCHEMA_PATH_PREFIX_TRIM stays False (default) so the path *keys* keep
    # /workspaces/api/ on both sides.
    from drf_spectacular.settings import spectacular_settings

    from stapel_workspaces._codegen_settings import CODEGEN_SCHEMA_PATH_PREFIX

    spectacular_settings.SCHEMA_PATH_PREFIX = CODEGEN_SCHEMA_PATH_PREFIX

    # The monolith's own codegen harness runs with DJANGO_ENV=local
    # (codegen/generate.sh), which makes its root urls.py include
    # get_dev_urls() -> get_swagger_urls() -> _register_jwt_auth_extension()
    # as a side effect of importing the URLconf — a *global* registration on
    # drf-spectacular's extension registry, not tied to any one module's
    # urls.py. stapel-auth's harness gets this for free only because its
    # co-mounted sibling (stapel_gdpr.urls) happens to call
    # get_app_swagger_urls() unconditionally; workspaces has no such sibling
    # (its monolith mount is workspaces-only — no closure gap per contract-
    # pipeline.md §9 Q2, confirmed: the $ref component closure is entirely
    # workspaces + StapelError). Without registering it explicitly here,
    # protected workspaces endpoints would emit without their monolith
    # `security: [{"JWTCookieAuth": []}]` entry — a real byte-identity delta,
    # not a component-closure gap.
    from stapel_core.django.openapi.swagger import _register_jwt_auth_extension

    _register_jwt_auth_extension()


def _require_python_312() -> None:
    """Abort emission if not running the pinned 3.12 interpreter.

    drf-spectacular's rendering of component descriptions (``Optional[X]`` vs
    ``X | None``) depends on the Python **minor** version — contracts emitted
    on anything other than 3.12 (the CI/monolith pin) produce false diffs
    against the committed docs/*.json. Emission must never proceed on the
    wrong minor.
    """
    if sys.version_info[:2] != (3, 12):
        got = f"{sys.version_info.major}.{sys.version_info.minor}"
        raise SystemExit(
            f"stapel-workspaces contract emission ABORTED: running Python "
            f"{got}, but contracts must be emitted on Python 3.12 (the "
            "CI/monolith pin). drf-spectacular renders component "
            "descriptions (Optional[X] vs X | None) differently across "
            "Python minor versions, so emitting on any other minor produces "
            "false diffs against the committed docs/*.json. Re-run under a "
            "3.12 interpreter."
        )


def main(argv: list[str] | None = None) -> int:
    _require_python_312()

    parser = argparse.ArgumentParser(
        prog="stapel-workspaces-contract",
        description="Emit this module's contract triad (schema.json + flows.json "
        "+ errors.json) into --out, canonical /workspaces/api/ prefix.",
    )
    parser.add_argument(
        "--out",
        default="docs",
        help="Output directory for the triad (default: docs).",
    )
    args = parser.parse_args(argv)

    _configure()

    # Reuse the shared mechanism's byte-stable emitters (contract-pipeline.md §2:
    # "the single-module harness already exists"). We call the three triad
    # emitters directly rather than generate(), which would also emit the
    # features/ Gherkin bundle — a separate concern this module does not ship.
    from stapel_tools.codegen import emit_errors, emit_flows, emit_schema

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    paths = emit_schema(out / "schema.json")
    flows = emit_flows(out / "flows.json")
    errors = emit_errors(out / "errors.json")

    print(
        f"stapel-workspaces contract: {paths} paths, {flows} flows, {errors} "
        f"error keys → {out}/",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
