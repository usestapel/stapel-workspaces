"""``ENSURE_LANDING_ON_LIST`` — the list read applies the street landing policy.

The ``user.registered`` subscriber that mints a personal workspace is
asynchronous. A client that reads the workspace list right after signup
races it; when it loses, it gets an empty list and settles on "no
workspace". With the key on, the first read itself creates the workspace
through the same idempotent primitive, so the list is never empty for a
fresh street account.
"""
import pytest
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from stapel_workspaces.models import Role, Workspace, WorkspaceMember, WorkspaceType
from stapel_workspaces.services import (
    _make_unique_slug,
    create_workspace,
    ensure_personal_workspace,
)

BASE = "/workspaces/api/workspaces/v1"
ON = {"ENSURE_LANDING_ON_LIST": True}


@pytest.fixture
def guest(db):
    from stapel_core.django.users.models import User

    return User.create_anonymous_user()


@pytest.fixture
def guest_client(api_client, guest):
    api_client.force_authenticate(user=guest)
    return api_client


@pytest.mark.django_db
class TestListEnsuresLanding:
    @override_settings(STAPEL_WORKSPACES=ON)
    def test_fresh_guest_first_read_returns_its_personal_workspace(
        self, guest_client, guest
    ):
        resp = guest_client.get(f"{BASE}/")
        assert resp.status_code == 200, resp.content
        body = resp.json()
        assert len(body["workspaces"]) == 1
        assert body["workspaces"][0]["type"] == WorkspaceType.PERSONAL
        assert body["is_guest"] is False
        ws = Workspace.objects.get(owner=guest)
        assert WorkspaceMember.objects.get(workspace=ws, user=guest).role == Role.OWNER

    @override_settings(STAPEL_WORKSPACES=ON)
    def test_read_and_subscriber_share_one_workspace(self, guest_client, guest):
        guest_client.get(f"{BASE}/")
        guest_client.get(f"{BASE}/")
        # The subscriber arriving after the read finds the same workspace.
        assert ensure_personal_workspace(guest).owner_id == guest.pk
        assert Workspace.objects.filter(owner=guest).count() == 1

    @override_settings(STAPEL_WORKSPACES=ON)
    def test_subscriber_first_then_read_adds_nothing(self, guest_client, guest):
        ws = ensure_personal_workspace(guest)
        resp = guest_client.get(f"{BASE}/")
        assert [w["id"] for w in resp.json()["workspaces"]] == [str(ws.id)]
        assert Workspace.objects.filter(owner=guest).count() == 1

    @override_settings(STAPEL_WORKSPACES=ON)
    def test_account_with_a_membership_gets_no_personal_workspace(
        self, authed_client, user, other_user
    ):
        org = create_workspace(user=other_user, name="Acme")
        WorkspaceMember.objects.create(
            workspace=org, user=user, role=Role.MEMBER, accepted_at=timezone.now()
        )
        resp = authed_client.get(f"{BASE}/")
        assert [w["id"] for w in resp.json()["workspaces"]] == [str(org.id)]
        assert not Workspace.objects.filter(owner=user).exists()

    @override_settings(STAPEL_WORKSPACES=ON)
    def test_pending_invitee_gets_no_personal_workspace(
        self, authed_client, user, other_user
    ):
        org = create_workspace(user=other_user, name="Acme")
        WorkspaceMember.objects.create(workspace=org, user=user, role=Role.MEMBER)
        authed_client.get(f"{BASE}/")
        assert not Workspace.objects.filter(owner=user).exists()

    @override_settings(
        STAPEL_WORKSPACES={"ENSURE_LANDING_ON_LIST": True, "STREET_LANDING_MODE": "none"}
    )
    def test_closed_landing_mode_still_mints_nothing(self, guest_client, guest):
        resp = guest_client.get(f"{BASE}/")
        assert resp.json()["workspaces"] == []
        assert not Workspace.objects.filter(owner=guest).exists()

    def test_off_by_default(self, guest_client, guest):
        resp = guest_client.get(f"{BASE}/")
        assert resp.json()["workspaces"] == []
        assert not Workspace.objects.filter(owner=guest).exists()

    @override_settings(STAPEL_WORKSPACES={"ENSURE_LANDING_ON_LIST": "false"})
    def test_env_style_false_string_is_off(self, guest_client, guest):
        guest_client.get(f"{BASE}/")
        assert not Workspace.objects.filter(owner=guest).exists()


@pytest.mark.django_db
class TestSlugCostIsBounded:
    def test_slug_probe_does_not_grow_with_namesakes(self, other_user):
        for _ in range(30):
            Workspace.objects.create(
                name="Personal",
                slug=_make_unique_slug("Personal"),
                type=WorkspaceType.PERSONAL,
                owner=other_user,
            )
        with CaptureQueriesContext(connection) as ctx:
            slug = _make_unique_slug("Personal")
        assert len(ctx.captured_queries) <= 3
        assert slug == "personal-31"
        assert len(slug) <= Workspace._meta.get_field("slug").max_length
        assert not Workspace.objects.filter(slug=slug).exists()

    def test_free_base_is_used_as_is(self, db):
        assert _make_unique_slug("Brand New Space") == "brand-new-space"
