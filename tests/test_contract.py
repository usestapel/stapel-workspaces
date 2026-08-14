"""Per-module contract triad + drift gate (contract-pipeline.md §2-3).

stapel-workspaces emits its **own** contract triad — ``docs/schema.json``
(drf-spectacular OpenAPI), ``docs/flows.json`` (generate_flow_docs machine
artifact — empty here, this module has no ``@flow_step`` annotations) and
``docs/errors.json`` (generate_error_keys registry) — from a single-module
``{workspaces + core}`` Django instance mounted at the canonical
``/workspaces/api/`` prefix. The frontend codegen consumes these committed
artifacts instead of the monolith aggregate.

The emitted schema/flows are **byte-identical to the monolith aggregate's
workspaces slice** (paths under ``/workspaces/api/`` + their transitive
component closure); see ``test_matches_monolith_workspaces_slice`` — the
guarantee the whole repoint rests on.

Regenerate after any change to a serializer / view / url / flow / error key:

    make contract        # or: python -m stapel_workspaces._codegen --out docs

then commit ``docs/{schema,flows,errors}.json``. Without regenerating, the drift
gate below fails — the same byte-stable regenerate-and-diff discipline as
``test_error_keys``.

The harness runs in a **subprocess**: this test process already configured Django
(via conftest, on the test-only double-mount urlconf), and the harness needs its
own canonical-prefix urlconf + drf-spectacular singleton — a clean interpreter
is the honest way to exercise exactly what ``make contract`` runs.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_PY = sys.version_info[:2]
if _PY != (3, 12):
    _GOT = f"{_PY[0]}.{_PY[1]}"
    _PY312_MSG = (
        "stapel-workspaces contract tests require Python 3.12 (the "
        f"CI/monolith pin) — running {_GOT}. drf-spectacular renders "
        "component descriptions (Optional[X] vs X | None) differently "
        "across Python minor versions, so drift/identity checks "
        "emitted+compared under any other minor produce false diffs."
    )
    pytest.skip(
        _PY312_MSG + " Skipping on any non-3.12 interpreter (CI or local) — "
        "the contract canon is only defined on Python 3.12.",
        allow_module_level=True,
    )

REPO = Path(__file__).resolve().parent.parent
DOCS = REPO / "docs"
TRIAD = ("schema.json", "flows.json", "errors.json")
# The fourth artifact (capability-config.md §2): STAPEL_WORKSPACES carries
# mostly merge-registries and tuning, plus ONE CTO-facing axis as of the
# mandate-model vardict (2026-08-03, org-program #85) — STREET_LANDING_MODE
# (personal|none, what an un-invited registration lands with). Emitted from
# the urls.py gate registry + schema.json + the curated
# docs/capabilities.meta.json. Same emit/drift discipline.
# The fifth artifact (badge-canon §3): docs/llms.txt, rendered from
# docs/capabilities.json (+schema/errors/flows) by stapel_tools.llms_txt.
#
# The mandate-model surface (permissions.py + capabilities.py + services.py —
# guest predicate, rank-guard, invitation/provision/suspension primitives)
# does not fit the generator's default 4000-token budget. Same exception
# stapel-auth already takes: raise the ceiling for this module, do not
# shorten intents to fit. Raised again in 0.19 (4500 -> 5000) when the
# profiles seam behind the roster's name edit added three surface entries
# and the profiles dependency line grew a write half. Raised again in 0.20
# (5000 -> 5500) for the two preferred-workspace surface entries, and again
# (5500 -> 6000) when the audit journal moved into the core event store and
# its sink seam, anchor read and migration data path each earned a surface
# entry. The budget stays enforced, just at 6000 — keep it in step with the
# Makefile.
ARTIFACTS = TRIAD + ("capabilities.json", "llms.txt")
LLMS_TXT_BUDGET = "6000"


def _emit(out_dir: Path) -> None:
    for module in ("stapel_workspaces._codegen", "stapel_workspaces._capabilities"):
        subprocess.run(
            [sys.executable, "-m", module, "--out", str(out_dir)],
            cwd=str(REPO),
            check=True,
            capture_output=True,
        )
    # llms.txt is rendered from the REAL committed docs/capabilities.json (not
    # the just-regenerated tmp one) — same as `make contract-check` — so this
    # step also catches a stale llms.txt independently of the loop above.
    subprocess.run(
        [
            sys.executable, "-m", "stapel_tools.llms_txt", ".",
            "--out", str(out_dir), "--budget", LLMS_TXT_BUDGET,
        ],
        cwd=str(REPO),
        check=True,
        capture_output=True,
    )


def test_contract_artifacts_committed():
    for name in ARTIFACTS:
        assert (DOCS / name).is_file(), f"missing docs/{name} — run `make contract`"
    assert (DOCS / "capabilities.meta.json").is_file(), (
        "missing docs/capabilities.meta.json — the curated layer is "
        "hand-written and committed, not generated"
    )


def test_contract_has_no_drift(tmp_path):
    """Regenerate into a temp dir; committed artifacts must match byte-for-byte."""
    _emit(tmp_path)
    for name in ARTIFACTS:
        committed = (DOCS / name).read_bytes()
        regenerated = (tmp_path / name).read_bytes()
        assert committed == regenerated, (
            f"docs/{name} drifted — run `make contract` and commit docs/{name}"
        )


def test_emission_is_deterministic(tmp_path):
    """Two independent emissions are byte-identical (drift gate is meaningful)."""
    a, b = tmp_path / "a", tmp_path / "b"
    _emit(a)
    _emit(b)
    for name in ARTIFACTS:
        assert (a / name).read_bytes() == (b / name).read_bytes()


def test_paths_carry_canonical_prefix():
    """The mount-prefix fix: schema paths + flow endpoints are /workspaces/api/*, not bare."""
    schema = json.loads((DOCS / "schema.json").read_text())
    assert schema["paths"], "schema has no paths"
    assert all(p.startswith("/workspaces/api/") for p in schema["paths"]), (
        "schema paths are not mounted at the canonical /workspaces/api/ prefix"
    )
    flows = json.loads((DOCS / "flows.json").read_text())
    for flow in flows:
        for step in flow.get("steps", []):
            for ep in step.get("endpoints", []):
                assert ep["path"].startswith("/workspaces/api/"), (
                    f"flow endpoint {ep['path']} is not canonically prefixed"
                )


# --- Byte-identity regression vs the monolith aggregate's workspaces slice ----
# Only runs in the workspace (the monolith is a sibling repo, absent in module CI).

_MONO = REPO.parent / "stapel-example-monolith" / "codegen" / "generated" / "schema.json"


def _closure(schema: dict, seeds: set[str]) -> set[str]:
    import re

    comps = schema["components"]["schemas"]
    seen: set[str] = set()
    stack = list(seeds)
    while stack:
        name = stack.pop()
        if name in seen or name not in comps:
            continue
        seen.add(name)
        for ref in re.findall(r'"#/components/schemas/([^"]+)"', json.dumps(comps[name])):
            stack.append(ref)
    return seen


def _refs(obj) -> set[str]:
    import re

    return set(re.findall(r'"#/components/schemas/([^"]+)"', json.dumps(obj)))


@pytest.mark.skipif(
    not _MONO.exists() or os.environ.get("STAPEL_SKIP_MONOLITH_IDENTITY"),
    reason="monolith aggregate not present (module CI checks out only this repo)",
)
def test_matches_monolith_workspaces_slice():
    """docs/schema.json == the monolith aggregate's /workspaces/api/ slice, byte-for-byte.

    Compares path objects and the transitive component closure — the envelope
    (info/servers) is intentionally not compared (it names workspaces, not the
    monolith).
    """
    mine = json.loads((DOCS / "schema.json").read_text())
    mono = json.loads(_MONO.read_text())

    mono_paths = {p: v for p, v in mono["paths"].items() if p.startswith("/workspaces/api/")}
    assert set(mine["paths"]) == set(mono_paths), "path set differs from monolith slice"
    for p in mono_paths:
        assert json.dumps(mine["paths"][p], sort_keys=True) == json.dumps(
            mono_paths[p], sort_keys=True
        ), f"path object {p} differs from monolith slice"

    seeds: set[str] = set()
    for v in mono_paths.values():
        seeds |= _refs(v)
    mono_cl = _closure(mono, seeds)
    my_seeds: set[str] = set()
    for v in mine["paths"].values():
        my_seeds |= _refs(v)
    my_cl = _closure(mine, my_seeds)
    assert mono_cl == my_cl, "component closure differs from monolith slice"
    for c in mono_cl:
        assert json.dumps(mine["components"]["schemas"][c], sort_keys=True) == json.dumps(
            mono["components"]["schemas"][c], sort_keys=True
        ), f"component {c} differs from monolith slice"


# --- capabilities.json content sanity (capability-config.md §2) ---------------


def _capabilities() -> dict:
    return json.loads((DOCS / "capabilities.json").read_text())


def test_capabilities_has_the_street_landing_mode_axis():
    """One CTO-facing axis (mandate-model vardict 2026-08-03, org-program #85).

    STREET_LANDING_MODE is the only settings key promoted to an axis — every
    other STAPEL_WORKSPACES key is a merge-registry (ROLES,
    CAPABILITY_LEVELS) or a tuning knob (capability-config.md §2).
    """
    axes = {a["key"]: a for a in _capabilities()["axes"]}
    assert set(axes) == {"STREET_LANDING_MODE"}
    assert axes["STREET_LANDING_MODE"]["default"] == "personal"
    assert axes["STREET_LANDING_MODE"]["kind"] == "enum"


def test_capabilities_operations_total_matches_schema():
    schema = json.loads((DOCS / "schema.json").read_text())
    methods = {"get", "put", "post", "delete", "options", "head", "patch", "trace"}
    total = sum(
        1 for item in schema["paths"].values() for m in item if m in methods
    )
    assert _capabilities()["operations_total"] == total


def test_capabilities_envelope():
    doc = _capabilities()
    import tomllib

    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text())
    assert doc["module"] == pyproject["project"]["name"]
    assert doc["version"] == pyproject["project"]["version"]
    assert doc["provides"]
    assert doc["extension_points"]
    assert doc["requires"]


def test_capabilities_stale_meta_axis_fails_loudly():
    """A curated axis entry for a module with no axes must be an emission ERROR."""
    from stapel_tools.capabilities import build_capabilities

    from stapel_workspaces.urls import GATE_REGISTRY

    schema = json.loads((DOCS / "schema.json").read_text())
    meta = json.loads((DOCS / "capabilities.meta.json").read_text())
    broken = json.loads(json.dumps(meta))
    broken["axes"]["WORKSPACES_NO_SUCH_AXIS"] = {"summary": "x", "business_label": "x"}

    with pytest.raises(SystemExit, match="WORKSPACES_NO_SUCH_AXIS"):
        build_capabilities(
            module="stapel-workspaces",
            version="0.0.0",
            defaults={},
            registry=GATE_REGISTRY,
            schema=schema,
            meta=broken,
            is_axis=lambda k: False,
            axis_group=lambda k: "unreachable",
            canonical_prefix="/workspaces/api",
        )


# --- README.md — the sixth artifact (tracker #257) ---------------------------
#
# README.md is assembled by ``stapel_tools.readme`` from docs/readme.md (the
# human half: what this module is and how to think about it) plus the contract
# documents above (badges, version, surface counts, doc links). Everything a
# hand-written README used to restate — and therefore used to get wrong one
# release later — is generated and gated here.

def test_readme_is_assembled_and_has_no_drift():
    from stapel_tools.readme import load_inputs, render, static_languages

    inputs = load_inputs(REPO)
    languages = static_languages(REPO)
    assert languages == ["en"], "expected exactly the English static body docs/readme.md"
    committed = (REPO / "README.md").read_text()
    assert committed == render(REPO, inputs, "en", languages), (
        "README.md drifted — run `make contract` and commit README.md "
        "(edit prose in docs/readme.md, never README.md itself)"
    )


def test_readme_version_matches_the_package():
    """The #226 gate, at the point where the number is published."""
    import tomllib

    from stapel_tools.readme import load_inputs, resolve_version

    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text())
    assert resolve_version(load_inputs(REPO)) == pyproject["project"]["version"]
