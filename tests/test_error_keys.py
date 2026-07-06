"""errors.json codegen artifact + drift gate (error-remediation task).

stapel-workspaces carries the backend ``errors.json`` artifact — the
language-agnostic registry of every ``error.<status>.<name>`` key the service
can raise, with its HTTP ``status``, ``{param}`` slots, machine-readable
``remediation`` hint, and canonical English text. The committed
``docs/errors.json`` must be exactly what ``generate_error_keys`` emits from the
live error registry — the same byte-stable regenerate-and-diff discipline as the
flow docs and schema.json (auth is the pilot; this mirrors its gate).

Regenerate after adding/changing an error key or its remediation:

    STAPEL_REGEN_ERROR_KEYS=1 python -m pytest \
        tests/test_error_keys.py::test_error_keys_have_no_drift

then commit ``docs/errors.json``. Without the env var the same test is the CI
drift gate: it regenerates into a temp dir and asserts byte-for-byte equality
with the committed artifact (a no-op regen is a no-op diff).
"""
import io
import json
import os
import re
from pathlib import Path

from django.core.management import call_command
from stapel_core.django.api.errors import REMEDIATION_VOCAB

ERRORS_JSON = Path(__file__).resolve().parent.parent / "docs" / "errors.json"


def _generate(out: Path) -> None:
    call_command("generate_error_keys", "--out", str(out), stdout=io.StringIO())


def test_error_keys_have_no_drift(tmp_path):
    if os.environ.get("STAPEL_REGEN_ERROR_KEYS"):
        _generate(ERRORS_JSON)
        return

    out = tmp_path / "errors.json"
    _generate(out)
    generated = out.read_bytes()
    committed = ERRORS_JSON.read_bytes()
    assert committed == generated, (
        "errors.json drifted — run "
        "STAPEL_REGEN_ERROR_KEYS=1 pytest tests/test_error_keys.py and commit "
        "docs/errors.json"
    )


def test_committed_artifact_shape():
    entries = json.loads(ERRORS_JSON.read_text())
    assert isinstance(entries, list) and entries
    codes = [e["code"] for e in entries]
    assert codes == sorted(codes), "entries must be sorted by code"
    assert len(codes) == len(set(codes)), "codes must be unique"
    for e in entries:
        assert set(e) == {"code", "status", "params", "remediation", "en"}
        assert e["code"].startswith("error.")
        assert e["status"] == int(e["code"].split(".")[1])
        assert isinstance(e["params"], list)
        assert e["remediation"] in REMEDIATION_VOCAB
        assert e["en"] and isinstance(e["en"], str)
        # Every `{param}` slot in the text is declared in params.
        slots = {m.group(1) for m in re.finditer(r"\{(\w+)\}", e["en"])}
        assert slots <= set(e["params"])


def test_service_keys_present_with_declared_remediation():
    entries = {e["code"]: e for e in json.loads(ERRORS_JSON.read_text())}
    # Workspaces-declared canon (backend overrides the frontend heuristic):
    #   * every *_not_found → fix_input (the heuristic retries a 404 not_found,
    #     which loops the failing lookup);
    #   * forbidden_workspace → contact_support (the not-a-member/authorization
    #     boundary: no field to fix, retry loops, an owner must invite/promote —
    #     heuristic would retry a 403);
    #   * last_owner_cannot_be_removed → fix_input (self-serve precondition,
    #     "transfer ownership first" — heuristic would retry a 403);
    #   * invitation_expired / invitation_revoked → contact_support (dead,
    #     immutable token; only the owner can re-invite — heuristic says retry
    #     for expired, fix_input for revoked, both wrong);
    #   * invitation_already_used → fix_input (benign double-submit; nothing to
    #     escalate, retry loops on a spent token — matches heuristic).
    expected = {
        "error.404.workspace_not_found": "fix_input",
        "error.404.member_not_found": "fix_input",
        "error.404.invitation_not_found": "fix_input",
        "error.403.forbidden_workspace": "contact_support",
        "error.403.last_owner_cannot_be_removed": "fix_input",
        "error.400.workspace_slug_taken": "fix_input",
        "error.400.already_workspace_member": "fix_input",
        "error.400.invitation_expired": "contact_support",
        "error.400.invitation_already_used": "fix_input",
        "error.400.invitation_revoked": "contact_support",
        "error.400.invalid_role": "fix_input",
    }
    for code, remediation in expected.items():
        assert entries[code]["remediation"] == remediation, code
    # Cross-cutting core keys (COMMON_ERRORS) are folded into the artifact.
    assert entries["error.404.not_found"]["remediation"] in REMEDIATION_VOCAB
