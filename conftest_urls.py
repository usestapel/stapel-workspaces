"""URL configuration used only during tests.

Mounts the module through its **root** urlconf (``stapel_workspaces.urls``),
the same include every host uses — so the suite exercises the *mounted*
contract, version segment and all: ``.../workspaces/v1/<route>``.

Why this matters (regression, 2026-07): this file used to include
``stapel_workspaces.urls_v1`` directly, skipping the ``v1/`` prefix that
``urls.py`` contributes. The suite was green against paths that do not exist
on the wire — a stapel-core client kept calling the pre-v1 internal
membership path, got a 404, cached it as "not a member", and the workspace
owner saw "Forbidden: not a member of this workspace" in production. Nothing
in the suite could catch it, because nothing in the suite went through
``urls.py``. Mount through the root urlconf only; the mounted contract itself
is pinned by ``tests/test_url_mount_contract.py``.

The host prefix (``workspaces/api/workspaces/``) reproduces the deployed
mount of the incident; the module-relative part (``v1/...``) is what the
module owns and what the contract fixes.
"""

from django.urls import include, path

urlpatterns = [
    path("workspaces/api/workspaces/", include("stapel_workspaces.urls")),
]
