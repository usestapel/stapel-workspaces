"""The guest (anonymous session) surface of stapel-workspaces.

With ``AUTH_ANONYMOUS`` on, a guest session is ``is_authenticated``, so a
bare ``IsAuthenticated`` gate lets it through — and until this module said
otherwise, nothing in the source stated whether that was wanted
(``stapel_core.adoption`` W002). ``views.py`` now states it per view; these
tests are what keeps the statement true.

Two halves, and the first is the one that would hurt in production:

* ``GET /`` (the workspace list) is on the **live guest path** — an app
  header asks it for every session to decide what to draw, guest included.
  A regression that closes it breaks the header for guests in production,
  not in review. The test below pins the empty list, not a 403.
* Everything workspace-scoped is already closed to a guest by the capability
  check in the method body, and the invitation views by the email match.
  The tests prove the *existing* gate really does hold for an anonymous
  session, so the ``ANONYMOUS_DENIED`` declarations are not aspirational.
"""


import pytest

from stapel_core.django.api.permissions import (
    ANONYMOUS_ALLOWED,
    ANONYMOUS_DENIED,
)
from stapel_workspaces import views
from stapel_workspaces.models import Role, Workspace, WorkspaceMember

BASE = "/workspaces/api/workspaces/v1"


@pytest.fixture
def guest(db):
    """A guest session's user — what ``POST /auth/api/v1/anonymous/`` mints:
    authenticated, ``is_anonymous=True``, and with no email at all."""
    from stapel_core.django.users.models import User

    return User.create_anonymous_user()


@pytest.fixture
def guest_client(api_client, guest):
    api_client.force_authenticate(user=guest)
    return api_client


def _create_ws(user, name="Acme", **kwargs):
    from stapel_workspaces.services import create_workspace

    return create_workspace(user=user, name=name, **kwargs)


# ---------------------------------------------------------------------------
# The guest path: the app header asks "which workspaces am I in?"
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestGuestMayListWorkspaces:
    def test_guest_gets_an_empty_list_not_a_refusal(self, guest_client):
        resp = guest_client.get(f"{BASE}/")
        assert resp.status_code == 200, resp.content
        assert resp.json() == {"workspaces": [], "is_guest": True}

    def test_guest_list_does_not_leak_other_peoples_workspaces(
        self, guest_client, user
    ):
        _create_ws(user, name="Someone else's")
        resp = guest_client.get(f"{BASE}/")
        assert resp.status_code == 200
        assert resp.json()["workspaces"] == []

    def test_declared_allowed_in_the_source(self):
        assert (
            views.WorkspaceListCreateView.stapel_anonymous_access == ANONYMOUS_ALLOWED
        )


@pytest.mark.django_db
class TestGuestMayNotCreateAWorkspace:
    """The other half of the same view — a workspace has an owner, and a
    throwaway account must not become one."""

    def test_guest_cannot_create_an_org(self, guest_client):
        resp = guest_client.post(f"{BASE}/", {"name": "Ghost Corp"}, format="json")
        assert resp.status_code == 403, resp.content
        assert not Workspace.objects.exists()

    def test_guest_cannot_create_a_personal_workspace_either(self, guest_client):
        """The entitlement seam never gates personal workspaces, so this half
        would be wide open if the guard were left to billing."""
        resp = guest_client.post(
            f"{BASE}/", {"name": "Mine", "type": "personal"}, format="json"
        )
        assert resp.status_code == 403, resp.content
        assert not Workspace.objects.exists()

    def test_registered_user_still_creates(self, authed_client):
        resp = authed_client.post(f"{BASE}/", {"name": "Acme"}, format="json")
        assert resp.status_code == 201, resp.content


# ---------------------------------------------------------------------------
# Deployment metadata stays readable
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_guest_may_read_the_role_registry(guest_client):
    resp = guest_client.get(f"{BASE}/roles")
    assert resp.status_code == 200, resp.content
    assert resp.json()["roles"], "the builtin roles must still come back"


# ---------------------------------------------------------------------------
# Everything workspace-scoped stays shut, by the gate that was already there
# ---------------------------------------------------------------------------


@pytest.fixture
def foreign_ws(user):
    """A workspace the guest has nothing to do with."""
    return _create_ws(user, name="Acme")


@pytest.mark.django_db
class TestGuestReachesNoWorkspace:
    def test_detail(self, guest_client, foreign_ws):
        assert guest_client.get(f"{BASE}/{foreign_ws.id}").status_code == 403

    def test_update(self, guest_client, foreign_ws):
        resp = guest_client.patch(
            f"{BASE}/{foreign_ws.id}", {"name": "Taken"}, format="json"
        )
        assert resp.status_code == 403
        foreign_ws.refresh_from_db()
        assert foreign_ws.name == "Acme"

    def test_member_list(self, guest_client, foreign_ws):
        assert guest_client.get(f"{BASE}/{foreign_ws.id}/members").status_code == 403

    def test_member_invite(self, guest_client, foreign_ws):
        resp = guest_client.post(
            f"{BASE}/{foreign_ws.id}/members/invite",
            {"emails": ["x@example.com"], "role": Role.MEMBER},
            format="json",
        )
        assert resp.status_code == 403

    def test_member_role_change(self, guest_client, foreign_ws, user):
        resp = guest_client.patch(
            f"{BASE}/{foreign_ws.id}/members/{user.id}",
            {"role": Role.MEMBER},
            format="json",
        )
        assert resp.status_code == 403
        assert (
            WorkspaceMember.objects.get(workspace=foreign_ws, user=user).role
            == Role.OWNER
        )

    def test_member_remove(self, guest_client, foreign_ws, user):
        resp = guest_client.delete(f"{BASE}/{foreign_ws.id}/members/{user.id}")
        assert resp.status_code == 403
        assert WorkspaceMember.objects.filter(workspace=foreign_ws, user=user).exists()

    def test_member_provision(self, guest_client, foreign_ws):
        resp = guest_client.post(
            f"{BASE}/{foreign_ws.id}/members/provision",
            {"username_local": "ghost", "role": Role.MEMBER},
            format="json",
        )
        assert resp.status_code in (401, 403), resp.content


# ---------------------------------------------------------------------------
# Invitations are personal, and a guest has no address to match
# ---------------------------------------------------------------------------


@pytest.fixture
def invitation(foreign_ws, user):
    from stapel_workspaces.services import create_invitation

    return create_invitation(
        workspace=foreign_ws,
        email="invitee@example.com",
        role=Role.MEMBER,
        invited_by=user,
    )


@pytest.mark.django_db
class TestGuestCannotResolveAnInvitation:
    def test_accept(self, guest_client, invitation, foreign_ws):
        resp = guest_client.post(
            f"{BASE}/invitations/accept", {"token": invitation.token}, format="json"
        )
        assert resp.status_code == 404, resp.content
        assert WorkspaceMember.objects.filter(workspace=foreign_ws).count() == 1

    def test_decline(self, guest_client, invitation):
        resp = guest_client.post(f"{BASE}/invitations/{invitation.token}/decline")
        assert resp.status_code == 404, resp.content
        invitation.refresh_from_db()
        assert invitation.declined_at is None


# ---------------------------------------------------------------------------
# No view is left silent
# ---------------------------------------------------------------------------


def test_declarations_match_the_source():
    denied = (
        views.WorkspaceDetailView,
        views.MemberListView,
        views.MemberInviteView,
        views.MemberProvisionView,
        views.MemberDetailView,
        views.InvitationAcceptView,
        views.InvitationDeclineView,
    )
    for view in denied:
        assert view.stapel_anonymous_access == ANONYMOUS_DENIED, view.__name__
    assert views.RoleListView.stapel_anonymous_access == ANONYMOUS_ALLOWED


def test_no_view_is_left_silent():
    """The question ``stapel_core.adoption`` E001/W002 asks a consumer's
    deployment, asked here — where it can be answered."""
    from rest_framework.permissions import IsAuthenticated
    from rest_framework.views import APIView

    from stapel_core.django.api.permissions import ANONYMOUS_DECLARATIONS

    silent = [
        name
        for name, obj in vars(views).items()
        if isinstance(obj, type)
        and issubclass(obj, APIView)
        and set(getattr(obj, "permission_classes", ()) or ()) == {IsAuthenticated}
        and getattr(obj, "stapel_anonymous_access", None) not in ANONYMOUS_DECLARATIONS
    ]
    assert silent == []
