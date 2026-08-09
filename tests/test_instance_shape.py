"""The instance shape ships publicly — to exactly the person who is no one
to the workspace.

WHY. The ``STREET_LANDING_MODE`` axis, added 2026-08-03, decides whether a
"street" arrival gets their own workspace (``personal`` — public cloud) or
lands as a guest without one (``none`` — closed deployment). It lived only
in backend config — no response exposed it, so a client had no way to tell
the two worlds apart.

The cost: the screen shown after being kicked from a workspace (Oleg's
request, 2026-08-08). In a closed deployment there is nowhere to go — no
workspace of one's own and none obtainable; offering "create a workspace"
there is a dead end drawn as a button. In a public cloud it's the opposite —
a workspace exists, and the client should route there.
"""
import pytest
from django.test import override_settings
from django.urls import reverse

pytestmark = pytest.mark.django_db


def _get(client):
    return client.get(reverse("instance-shape"))


class TestAxisReachesTheClient:
    @override_settings(STAPEL_WORKSPACES={"STREET_LANDING_MODE": "none"})
    def test_closed_deployment_is_visible(self, api_client):
        response = _get(api_client)
        assert response.status_code == 200
        assert response.data["landing"] == "none"

    @override_settings(STAPEL_WORKSPACES={"STREET_LANDING_MODE": "personal"})
    def test_public_cloud_is_visible(self, api_client):
        assert _get(api_client).data["landing"] == "personal"

    def test_default_is_public_cloud(self, api_client):
        """Axis unset — behavior predates the setting, byte for byte."""
        assert _get(api_client).data["landing"] == "personal"


class TestRegistrationIsTheSameAxis:
    """Two sides of the same decision — the client needs both."""

    @override_settings(STAPEL_WORKSPACES={"STREET_LANDING_MODE": "none"})
    def test_closed_deployment_has_no_registration(self, api_client):
        assert _get(api_client).data["registration_open"] is False

    @override_settings(STAPEL_WORKSPACES={"STREET_LANDING_MODE": "personal"})
    def test_public_cloud_has_registration_open(self, api_client):
        assert _get(api_client).data["registration_open"] is True


class TestOpenToAnonymousOnPurpose:
    def test_responds_without_authorization(self, api_client):
        """The only audience for this endpoint is someone WITHOUT access to
        the workspace.

        Requiring auth would lock out exactly the person it exists for:
        someone kicked out of the workspace, or who left on their own.
        """
        response = _get(api_client)
        assert response.status_code == 200
        assert set(response.data) == {"landing", "registration_open"}

    def test_leaks_nothing_beyond_the_shape(self, api_client):
        """The response is a DEPLOYMENT property, not user data.

        Pinned so a field added here "along the way" doesn't silently ship
        to anonymous callers: the endpoint is public, so any new field on it
        is public by definition.
        """
        payload = _get(api_client).data
        assert list(payload) == ["landing", "registration_open"]
