"""Display name closes the gap the meettoday frontend audit found (2026-08-04):

* the invite modal has an "Имя" field that went nowhere — ``MemberInviteRequest``
  only carried ``{emails, role}``;
* the member list had nothing to show but email — ``MemberResponse`` never
  carried a name at all.

The name itself is NOT invented here: it lives in stapel-profiles
(docs/llms.txt). This module stores only a HINT typed at invite/provision
time (``display_name_hint``, on both ``WorkspaceInvitation`` and
``WorkspaceMember``) — copied onto the membership exactly once, at creation,
never touched again — and ``MemberResponse.display_name`` prefers a live
stapel-profiles lookup (``services._fetch_profile_display_names``, best-effort
HTTP via the flat setting ``PROFILES_SERVICE_URL``) over that hint. With
``PROFILES_SERVICE_URL`` unset (the default in every test here) the lookup is
skipped outright — no network attempt, deterministic — so these tests pin
the hint-fallback path; ``TestFetchProfileDisplayNames`` covers the HTTP path
in isolation.
"""
import pytest
from django.utils import timezone

from stapel_workspaces import services
from stapel_workspaces.models import Role, WorkspaceMember

BASE = "/workspaces/api/workspaces/v1"
ACCEPT = f"{BASE}/invitations/accept"


def _create_ws(user, name="Acme"):
    return services.create_workspace(user=user, name=name)


def _invite_url(ws):
    return f"{BASE}/{ws.id}/members/invite"


def _accept(client, token):
    return client.post(ACCEPT, {"token": token}, format="json")


@pytest.mark.django_db
class TestInviteCarriesDisplayName:
    def test_invite_with_display_name_is_stored_and_returned(
        self, authed_client, user
    ):
        ws = _create_ws(user)
        resp = authed_client.post(
            _invite_url(ws),
            {
                "emails": ["ada@example.com"],
                "role": "member",
                "display_name": "Ada Lovelace",
            },
            format="json",
        )
        assert resp.status_code == 201, resp.content
        inv_data = resp.json()["invitations"][0]
        assert inv_data["display_name"] == "Ada Lovelace"
        inv = ws.invitations.get()
        assert inv.display_name_hint == "Ada Lovelace"

    def test_invite_without_display_name_behaves_as_before(
        self, authed_client, user
    ):
        ws = _create_ws(user)
        resp = authed_client.post(
            _invite_url(ws),
            {"emails": ["noname@example.com"], "role": "member"},
            format="json",
        )
        assert resp.status_code == 201, resp.content
        inv_data = resp.json()["invitations"][0]
        assert inv_data["display_name"] is None
        assert ws.invitations.get().display_name_hint == ""

    def test_invite_display_name_is_trimmed(self, authed_client, user):
        ws = _create_ws(user)
        resp = authed_client.post(
            _invite_url(ws),
            {
                "emails": ["pad@example.com"],
                "role": "member",
                "display_name": "  Grace Hopper  ",
            },
            format="json",
        )
        assert resp.status_code == 201, resp.content
        assert resp.json()["invitations"][0]["display_name"] == "Grace Hopper"

    def test_invite_display_name_too_long_rejected(self, authed_client, user):
        ws = _create_ws(user)
        resp = authed_client.post(
            _invite_url(ws),
            {
                "emails": ["toolong@example.com"],
                "role": "member",
                "display_name": "x" * 36,
            },
            format="json",
        )
        assert resp.status_code == 400
        assert ws.invitations.count() == 0

    def test_invite_blank_display_name_normalizes_to_none(
        self, authed_client, user
    ):
        ws = _create_ws(user)
        resp = authed_client.post(
            _invite_url(ws),
            {
                "emails": ["blank@example.com"],
                "role": "member",
                "display_name": "   ",
            },
            format="json",
        )
        assert resp.status_code == 201, resp.content
        assert resp.json()["invitations"][0]["display_name"] is None


@pytest.mark.django_db
class TestAcceptDoesNotLoseDisplayName:
    def test_accept_carries_the_hint_onto_the_member_and_response(
        self, api_client, user, other_user
    ):
        ws = _create_ws(user)
        inv = services.create_invitation(
            workspace=ws,
            email=other_user.email,
            role=Role.MEMBER,
            invited_by=user,
            display_name="Ada Lovelace",
        )
        api_client.force_authenticate(user=other_user)
        resp = _accept(api_client, inv.token)
        assert resp.status_code == 200, resp.content
        assert resp.json()["display_name"] == "Ada Lovelace"

        member = WorkspaceMember.objects.get(workspace=ws, user=other_user)
        assert member.display_name_hint == "Ada Lovelace"

    def test_accept_without_display_name_shows_none(
        self, api_client, user, other_user
    ):
        ws = _create_ws(user)
        inv = services.create_invitation(
            workspace=ws, email=other_user.email, role=Role.MEMBER, invited_by=user
        )
        api_client.force_authenticate(user=other_user)
        resp = _accept(api_client, inv.token)
        assert resp.status_code == 200, resp.content
        assert resp.json()["display_name"] is None

    def test_re_accept_does_not_clobber_an_existing_members_name_hint(
        self, api_client, user, other_user
    ):
        """A second invitation to an already-accepted member must not
        overwrite the name the member already carries — get_or_create's
        ``defaults`` never touch an existing row, and this pins that."""
        ws = _create_ws(user)
        first = services.create_invitation(
            workspace=ws,
            email=other_user.email,
            role=Role.MEMBER,
            invited_by=user,
            display_name="Original Name",
        )
        api_client.force_authenticate(user=other_user)
        assert _accept(api_client, first.token).status_code == 200

        second = services.create_invitation(
            workspace=ws,
            email=other_user.email,
            role=Role.ADMIN,
            invited_by=user,
            display_name="A Different Name",
        )
        resp = _accept(api_client, second.token)
        assert resp.status_code == 200, resp.content
        assert resp.json()["display_name"] == "Original Name"


@pytest.mark.django_db
class TestMemberListShowsDisplayName:
    def test_member_list_shows_the_invite_time_hint(
        self, authed_client, user, other_user
    ):
        ws = _create_ws(user)
        WorkspaceMember.objects.create(
            workspace=ws,
            user=other_user,
            role=Role.MEMBER,
            accepted_at=timezone.now(),
            display_name_hint="Ada Lovelace",
        )
        resp = authed_client.get(f"{BASE}/{ws.id}/members")
        assert resp.status_code == 200, resp.content
        by_user = {m["user_id"]: m for m in resp.json()["items"]}
        assert by_user[str(other_user.id)]["display_name"] == "Ada Lovelace"

    def test_member_list_shows_none_without_a_hint(self, authed_client, user):
        ws = _create_ws(user)
        resp = authed_client.get(f"{BASE}/{ws.id}/members")
        assert resp.status_code == 200, resp.content
        owner_row = next(
            m for m in resp.json()["items"] if m["user_id"] == str(user.id)
        )
        assert owner_row["display_name"] is None


@pytest.mark.django_db
class TestFetchProfileDisplayNames:
    """``services._fetch_profile_display_names`` — the best-effort HTTP read,
    isolated from the member/invite flow above (which never reaches the
    network with ``PROFILES_SERVICE_URL`` unset)."""

    def test_unset_url_skips_the_network_entirely(self, settings, monkeypatch):
        settings.PROFILES_SERVICE_URL = ""
        called = []
        monkeypatch.setattr(
            services.requests, "post", lambda *a, **k: called.append(1)
        )
        assert services._fetch_profile_display_names([user_id_stub()]) == {}
        assert called == []

    def test_empty_ids_short_circuits(self, settings):
        settings.PROFILES_SERVICE_URL = "http://stapel-profiles:8000"
        assert services._fetch_profile_display_names([]) == {}

    def test_happy_path_returns_names_keyed_by_user_id(self, settings, monkeypatch):
        settings.PROFILES_SERVICE_URL = "http://stapel-profiles:8000"
        uid = "550e8400-e29b-41d4-a716-446655440000"

        class _Resp:
            status_code = 200
            headers = {"Content-Type": "application/json"}

            def json(self):
                return {
                    "profiles": [{"user_id": uid, "display_name": "Ada Lovelace"}],
                    "missing": [],
                }

        monkeypatch.setattr(services.requests, "post", lambda *a, **k: _Resp())
        assert services._fetch_profile_display_names([uid]) == {uid: "Ada Lovelace"}

    def test_empty_display_name_is_absent_not_a_placeholder(
        self, settings, monkeypatch
    ):
        settings.PROFILES_SERVICE_URL = "http://stapel-profiles:8000"
        uid = "550e8400-e29b-41d4-a716-446655440000"

        class _Resp:
            status_code = 200
            headers = {"Content-Type": "application/json"}

            def json(self):
                return {
                    "profiles": [{"user_id": uid, "display_name": ""}],
                    "missing": [],
                }

        monkeypatch.setattr(services.requests, "post", lambda *a, **k: _Resp())
        assert services._fetch_profile_display_names([uid]) == {}

    def test_transport_error_degrades_to_empty(self, settings, monkeypatch):
        settings.PROFILES_SERVICE_URL = "http://stapel-profiles:8000"

        def _boom(*a, **k):
            raise services.requests.exceptions.ConnectionError("down")

        monkeypatch.setattr(services.requests, "post", _boom)
        assert services._fetch_profile_display_names(["x"]) == {}

    def test_routing_404_degrades_to_empty(self, settings, monkeypatch):
        """A 404 from the URL resolver (HTML body) is never a verdict —
        same ``service_answered`` discipline every other peer client in
        this fleet uses."""
        settings.PROFILES_SERVICE_URL = "http://stapel-profiles:8000"

        class _Resp:
            status_code = 404
            headers = {"Content-Type": "text/html"}

        monkeypatch.setattr(services.requests, "post", lambda *a, **k: _Resp())
        assert services._fetch_profile_display_names(["x"]) == {}


def user_id_stub():
    return "550e8400-e29b-41d4-a716-446655440000"
