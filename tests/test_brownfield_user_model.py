"""Regression guard: workspaces FKs honor a custom AUTH_USER_MODEL.

Gap #4 / brownfield adoption. Before 0.3.3 the workspaces models referenced the
concrete ``stapel_core.django.users.models.User`` directly, so a host with a
custom ``accounts.User(AbstractStapelUser)`` as AUTH_USER_MODEL got a ValueError
when creating a WorkspaceMember. The FKs now use ``settings.AUTH_USER_MODEL``.

The behavioral proof needs a *different* AUTH_USER_MODEL than the default suite,
which cannot be swapped in-process after Django's app registry loads — so it runs
out-of-process via ``brownfield_users/_runner.py``.
"""

import subprocess
import sys
from pathlib import Path

from django.conf import settings


def test_fks_target_swappable_user_model():
    """In-process sanity: the FK targets are the swappable AUTH_USER_MODEL string,
    not a concrete class pinned at import time."""
    from stapel_workspaces.models import Workspace, WorkspaceInvitation, WorkspaceMember

    for model, field in [
        (Workspace, "owner"),
        (WorkspaceMember, "user"),
        (WorkspaceMember, "invited_by"),
        (WorkspaceInvitation, "invited_by"),
    ]:
        f = model._meta.get_field(field)
        # deconstruct() emits the swappable setting reference for user-model FKs.
        _, _, _, kwargs = f.deconstruct()
        # deconstruct() lowercases the model label; compare case-insensitively.
        assert kwargs["to"].lower() == settings.AUTH_USER_MODEL.lower(), (
            model,
            field,
            kwargs["to"],
        )
        assert f.swappable_setting == "AUTH_USER_MODEL", (model, field)


def test_brownfield_custom_user_can_be_workspace_member():
    """A host whose AUTH_USER_MODEL is a custom AbstractStapelUser subclass can
    own a workspace and be a member — the case that raised ValueError pre-0.3.3."""
    runner = Path(__file__).parent / "brownfield_users" / "_runner.py"
    proc = subprocess.run(
        [sys.executable, str(runner)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    assert "BROWNFIELD_OK" in proc.stdout, proc.stdout
