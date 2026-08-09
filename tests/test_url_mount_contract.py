"""The mounted-URL contract: every route as the network sees it.

Why this file exists (incident, 2026-07): ``urls.py`` mounts the whole API
under a ``v1/`` prefix, but ``conftest_urls.py`` used to ``include`` the
inner ``urls_v1`` module *directly*, so the suite never crossed the mount.
Every test asserted against pre-v1 paths that return 404 in production, and
the suite stayed green while the deployed contract was broken: a stapel-core
client kept calling the pre-v1 internal membership path
(``.../workspaces/internal/{ws}/members/{uid}``), got a 404, cached it as
"not a member", and the workspace owner was told "Forbidden: not a member of
this workspace".

So this file pins the *full* mounted path of every route by name, not the
route relative to some inner urlconf. Move the mount (drop ``v1/``, rename
it, re-nest it) and these assertions go red first. ``ROUTES`` must list every
pattern in ``urls_v1`` — the completeness check below fails on a new route
that nobody pinned.

The host prefix (``/workspaces/api/workspaces``) is the test harness's mount
(``conftest_urls.py``, reproducing the deployment of the incident); the
``/v1/...`` tail is what this module owns and ships.
"""

import pytest
from django.urls import Resolver404, resolve, reverse

# Host mount from conftest_urls.py; the module contributes everything after it.
MOUNT = "/workspaces/api/workspaces"

WS = "11111111-1111-1111-1111-111111111111"
UID = "22222222-2222-2222-2222-222222222222"
INV = "33333333-3333-3333-3333-333333333333"
TOKEN = "sometoken"

#: (url name, reverse kwargs, full path as the network sees it).
ROUTES = [
    ("workspace-list", {}, f"{MOUNT}/v1/"),
    ("workspace-roles", {}, f"{MOUNT}/v1/roles"),
    # Instance shape — public, unauthenticated: read by someone who is no
    # longer anyone to the workspace (kicked out or left on their own).
    ("instance-shape", {}, f"{MOUNT}/v1/instance"),
    # The caller's own stated home workspace — user-scoped, so it is a
    # literal route above the `<uuid:workspace_id>` block, not under one.
    ("workspace-preferred", {}, f"{MOUNT}/v1/me/preferred-workspace"),
    ("workspace-detail", {"workspace_id": WS}, f"{MOUNT}/v1/{WS}"),
    ("workspace-members", {"workspace_id": WS}, f"{MOUNT}/v1/{WS}/members"),
    (
        "workspace-member-invite",
        {"workspace_id": WS},
        f"{MOUNT}/v1/{WS}/members/invite",
    ),
    (
        "workspace-member-provision",
        {"workspace_id": WS},
        f"{MOUNT}/v1/{WS}/members/provision",
    ),
    (
        "workspace-member-detail",
        {"workspace_id": WS, "user_id": UID},
        f"{MOUNT}/v1/{WS}/members/{UID}",
    ),
    # Roster-side name correction (0.19) — a suffix route on the member,
    # so the target is a membership of THIS workspace, never a bare user id.
    (
        "workspace-member-name",
        {"workspace_id": WS, "user_id": UID},
        f"{MOUNT}/v1/{WS}/members/{UID}/name",
    ),
    # Administrative password reset (#110) — nested under the member, so
    # the target is a membership in a workspace-scoped path.
    (
        "workspace-member-password-reset",
        {"workspace_id": WS, "user_id": UID},
        f"{MOUNT}/v1/{WS}/members/{UID}/password/reset",
    ),
    # Admin-side invitation surface (#109) — workspace-scoped and
    # capability-gated, unlike the token-addressed public routes below.
    (
        "workspace-invitation-list",
        {"workspace_id": WS},
        f"{MOUNT}/v1/{WS}/invitations",
    ),
    (
        "workspace-invitation-revoke",
        {"workspace_id": WS, "invitation_id": INV},
        f"{MOUNT}/v1/{WS}/invitations/{INV}/revoke",
    ),
    # Same name correction, one step earlier: the pending invitation's hint.
    (
        "workspace-invitation-name",
        {"workspace_id": WS, "invitation_id": INV},
        f"{MOUNT}/v1/{WS}/invitations/{INV}/name",
    ),
    (
        "workspace-invitation-resend",
        {"workspace_id": WS, "invitation_id": INV},
        f"{MOUNT}/v1/{WS}/invitations/{INV}/resend",
    ),
    ("workspace-invitation-accept", {}, f"{MOUNT}/v1/invitations/accept"),
    (
        "workspace-invitation-preview",
        {"token": TOKEN},
        f"{MOUNT}/v1/invitations/{TOKEN}",
    ),
    (
        "workspace-invitation-decline",
        {"token": TOKEN},
        f"{MOUNT}/v1/invitations/{TOKEN}/decline",
    ),
    (
        "workspace-invitation-claim",
        {"token": TOKEN},
        f"{MOUNT}/v1/invitations/{TOKEN}/claim",
    ),
    # The route the incident was about.
    (
        "workspace-internal-membership",
        {"workspace_id": WS, "user_id": UID},
        f"{MOUNT}/v1/internal/{WS}/members/{UID}",
    ),
    (
        "workspace-internal-personal",
        {"user_id": UID},
        f"{MOUNT}/v1/internal/users/{UID}/personal",
    ),
    # Error-key registry polled by stapel-translate's error_collector. It was
    # declared (WorkspacesErrorKeysView) but mounted nowhere until 2026-07-26,
    # so the collector's whole endpoint class did not exist in any service.
    ("error-keys", {}, f"{MOUNT}/v1/error-keys/"),
]


@pytest.mark.parametrize("name,kwargs,path", ROUTES, ids=[r[0] for r in ROUTES])
def test_route_reverses_to_mounted_path(name, kwargs, path):
    assert reverse(name, kwargs=kwargs) == path


@pytest.mark.parametrize("name,kwargs,path", ROUTES, ids=[r[0] for r in ROUTES])
def test_mounted_path_resolves_back(name, kwargs, path):
    """The path a client types must hit the view — not just reverse cleanly."""
    assert resolve(path).view_name == name


@pytest.mark.parametrize(
    "path",
    [r[2].replace("/v1/", "/", 1) for r in ROUTES],
)
def test_pre_v1_path_is_not_routed(path):
    """The exact 404 shape of the incident: no route answers without ``v1/``.

    Kept as a separate assertion so a re-introduced compatibility mount is a
    deliberate, visible change rather than a silent widening of the contract.
    """
    with pytest.raises(Resolver404):
        resolve(path)


def test_every_route_is_pinned():
    """A new route in urls_v1 must be added here, or the contract is unpinned."""
    from stapel_workspaces import urls_v1

    declared = {p.name for p in urls_v1.urlpatterns}
    assert declared == {name for name, _, _ in ROUTES}


def test_mount_carries_the_version_segment():
    """Guards the mount itself: urls.py is what contributes ``v1/``.

    Reversing through the root urlconf is the only reason the paths above
    carry a version at all — the inner urls_v1 patterns have none.
    """
    from stapel_workspaces import urls_v1

    for pattern in urls_v1.urlpatterns:
        assert not str(pattern.pattern).startswith("v1/"), (
            "version segment belongs to urls.py, not urls_v1"
        )
    assert reverse("workspace-list").startswith(f"{MOUNT}/v1/")
