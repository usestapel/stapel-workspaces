"""The instance names its default workspace; clients stop guessing.

Measured on the meettoday stand (2026-08-06): the frontend took
``workspaces[0]`` — the first row of a list ordered by ``-last_accessed_at``
— as "the active workspace". A person who belongs to two spaces therefore
landed in whichever they had touched last. The owner's four pending
invitations sat in the org space while his screen showed his PERSONAL one,
and it read as "the owner cannot see his own invitations".

The list response now carries the instance's declared default, so every
client resolves the same way instead of inventing a rule.
"""
import uuid

import pytest

from stapel_workspaces.services import create_workspace

URL = "/workspaces/api/workspaces/v1/"


@pytest.mark.django_db
class TestDefaultWorkspaceAxis:
    def test_absent_by_default(self, authed_client, user):
        """A deployment that declares nothing gets "" — not a guess."""
        create_workspace(user=user, name="Personal")
        assert authed_client.get(URL).json()["default_workspace_id"] == ""

    def test_declared_default_is_echoed_to_a_member(
        self, authed_client, user, settings
    ):
        personal = create_workspace(user=user, name="Personal")
        org = create_workspace(user=user, name="Org")
        settings.STAPEL_WORKSPACES = {"DEFAULT_WORKSPACE_ID": str(org.id)}
        body = authed_client.get(URL).json()
        assert body["default_workspace_id"] == str(org.id)
        # and it is NOT merely whichever row came first — the point of the key
        assert {w["id"] for w in body["workspaces"]} == {str(personal.id), str(org.id)}

    def test_a_non_member_is_never_pointed_at_it(
        self, authed_client, user, other_user, settings
    ):
        """Naming a workspace the caller cannot open would trade one wrong
        screen for another."""
        create_workspace(user=user, name="Mine")
        theirs = create_workspace(user=other_user, name="Theirs")
        settings.STAPEL_WORKSPACES = {"DEFAULT_WORKSPACE_ID": str(theirs.id)}
        assert authed_client.get(URL).json()["default_workspace_id"] == ""

    def test_an_id_that_does_not_exist_is_not_echoed(
        self, authed_client, user, settings
    ):
        create_workspace(user=user, name="Personal")
        settings.STAPEL_WORKSPACES = {"DEFAULT_WORKSPACE_ID": str(uuid.uuid4())}
        assert authed_client.get(URL).json()["default_workspace_id"] == ""

    def test_uuid_and_string_actually_compare(self, authed_client, user, settings):
        """Regression pin: `w.id` is a UUID and the setting is a string, and
        `UUID(...) == "a8bb..."` is False in Python. Compared naively the key
        would have silently never matched — the same shape of defect it
        exists to remove."""
        ws = create_workspace(user=user, name="Personal")
        assert ws.id != str(ws.id)  # the trap itself, stated
        settings.STAPEL_WORKSPACES = {"DEFAULT_WORKSPACE_ID": str(ws.id)}
        assert authed_client.get(URL).json()["default_workspace_id"] == str(ws.id)
