"""The profiles seam against a REAL stapel-profiles, in one process.

Opt-in second session — ``STAPEL_WORKSPACES_TEST_PROFILES=1 pytest
tests/test_profiles_comounted.py`` — because mounting the sibling into the
default suite registers ITS error keys into the process-global service
registry that this module's i18n catalog and error-key gates read (see
``_codegen_settings.profiles_co_mounted``). CI runs both sessions.

Why the second session has to exist at all: every production defect this
fleet has paid for on this seam was a **seam** defect, not a component one.
The read half of this very seam shipped green for months while a monolith
with profiles sitting next to it got ``{}`` every time, because both sides
were only ever tested in isolation. Fakes cannot answer "does a real
Profile row take this write" — a real row can.

Since 0.21.0 the seam is comm: this session registers stapel-profiles'
REAL providers (``profiles.set_display_name`` and friends, from its own
``apps.ready()``) and the in-process transport routes to them, so what runs
here is the monolith half of the same one mechanism a split deployment
uses over the wire. Nothing in this file changed when the transport did,
which is the point of a name-addressed seam.

What is proved here and nowhere else:

* the roster's write lands on ``stapel_profiles``' actual ``Profile``
  table, on the row of the named user, creating it when absent;
* the swap-aware resolution (``get_profile_model``) returns a usable model
  and ``save(update_fields=...)`` names columns that exist;
* ``profile.changed`` is really emitted, with the new name in the payload;
* the read half sees the row the write half just made — the roster shows
  the correction back;
* the co-mounted deployment needs no route configuration to do any of it.
"""
import uuid

import pytest
from django.apps import apps
from django.utils import timezone

from stapel_workspaces import services
from stapel_workspaces.models import Role, WorkspaceMember

pytestmark = pytest.mark.skipif(
    not apps.is_installed("stapel_profiles"),
    reason="stapel-profiles is not co-mounted — run with "
    "STAPEL_WORKSPACES_TEST_PROFILES=1 (see _codegen_settings)",
)

BASE = "/workspaces/api/workspaces/v1"


def _member_url(ws_id, user_id):
    return f"{BASE}/{ws_id}/members/{user_id}/name"


def _profile_model():
    from stapel_profiles.models import get_profile_model

    return get_profile_model()


def _add_member(ws, user, role=Role.MEMBER):
    return WorkspaceMember.objects.create(
        workspace=ws, user=user, role=role, accepted_at=timezone.now()
    )


@pytest.fixture
def stapel_error_envelope(settings):
    settings.REST_FRAMEWORK = {
        "EXCEPTION_HANDLER": "stapel_core.django.api.errors.stapel_exception_handler",
    }


@pytest.mark.django_db
class TestNameEditAgainstRealProfiles:
    def test_the_write_creates_the_row_and_stores_the_name(
        self, authed_client, user, other_user
    ):
        ws = services.create_workspace(user=user, name="Acme")
        _add_member(ws, other_user)
        Profile = _profile_model()
        assert not Profile.objects.filter(user_id=other_user.id).exists()

        resp = authed_client.patch(
            _member_url(ws.id, other_user.id),
            {"display_name": "Ada Lovelace"},
            format="json",
        )

        assert resp.status_code == 200, resp.content
        assert Profile.objects.get(user_id=other_user.id).display_name == "Ada Lovelace"

    def test_the_write_updates_an_existing_row(
        self, authed_client, user, other_user
    ):
        ws = services.create_workspace(user=user, name="Acme")
        _add_member(ws, other_user)
        Profile = _profile_model()
        Profile.objects.create(user_id=other_user.id, display_name="Typo Name")

        authed_client.patch(
            _member_url(ws.id, other_user.id),
            {"display_name": "Ada Lovelace"},
            format="json",
        )

        assert Profile.objects.get(user_id=other_user.id).display_name == "Ada Lovelace"
        assert Profile.objects.filter(user_id=other_user.id).count() == 1

    def test_the_write_publishes_profile_changed_with_the_new_name(
        self, authed_client, user, other_user
    ):
        seen = []

        from stapel_core.comm import on_action

        @on_action("profile.changed")
        def _capture(event):  # pragma: no cover - invoked through the bus
            seen.append(event.payload)

        ws = services.create_workspace(user=user, name="Acme")
        _add_member(ws, other_user)

        authed_client.patch(
            _member_url(ws.id, other_user.id),
            {"display_name": "Ada Lovelace"},
            format="json",
        )

        assert [p["display_name"] for p in seen] == ["Ada Lovelace"]
        assert seen[0]["user_id"] == str(other_user.id)

    def test_the_roster_reads_back_the_name_the_roster_wrote(
        self, authed_client, user, other_user
    ):
        """Both halves of the seam, against the same real table.

        The read half (``_fetch_profile_display_names``) and the write half
        must agree about where the canonical name lives — the class of
        disagreement that had a monolith showing bare email addresses while
        profiles held the names all along.
        """
        ws = services.create_workspace(user=user, name="Acme")
        _add_member(ws, other_user)

        authed_client.patch(
            _member_url(ws.id, other_user.id),
            {"display_name": "Ada Lovelace"},
            format="json",
        )

        roster = authed_client.get(f"{BASE}/{ws.id}/members").json()
        names = {m["user_id"]: m["display_name"] for m in roster["items"]}
        assert names[str(other_user.id)] == "Ada Lovelace"

    def test_clearing_the_name_really_empties_the_column(
        self, authed_client, user, other_user
    ):
        ws = services.create_workspace(user=user, name="Acme")
        _add_member(ws, other_user)
        Profile = _profile_model()
        Profile.objects.create(user_id=other_user.id, display_name="Old Name")

        resp = authed_client.patch(
            _member_url(ws.id, other_user.id), {"display_name": ""}, format="json"
        )

        assert resp.status_code == 200, resp.content
        assert Profile.objects.get(user_id=other_user.id).display_name == ""

    def test_the_real_canon_refuses_and_nothing_is_written(
        self, authed_client, user, other_user, stapel_error_envelope
    ):
        ws = services.create_workspace(user=user, name="Acme")
        _add_member(ws, other_user)
        Profile = _profile_model()

        resp = authed_client.patch(
            _member_url(ws.id, other_user.id),
            {"display_name": "Ada \U0001f600"},
            format="json",
        )

        assert resp.status_code == 400
        assert resp.json()["localizable_error"] == "error.400.display_name_emoji"
        assert not Profile.objects.filter(user_id=other_user.id).exists()

    def test_an_unknown_user_is_never_given_a_profile_row(
        self, authed_client, user
    ):
        """The 404 must land before anything is created.

        A rename endpoint that get_or_creates first would mint profile rows
        for arbitrary UUIDs on refused requests.
        """
        ws = services.create_workspace(user=user, name="Acme")
        stranger = uuid.uuid4()
        Profile = _profile_model()

        resp = authed_client.patch(
            _member_url(ws.id, stranger), {"display_name": "Ada Lovelace"},
            format="json",
        )

        assert resp.status_code == 404
        assert not Profile.objects.filter(user_id=stranger).exists()
