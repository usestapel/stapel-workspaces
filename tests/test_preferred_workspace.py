"""The person says where home is; the server remembers it.

`DEFAULT_WORKSPACE_ID` shipped in 0.18 with a promise in its own docstring —
"a DEFAULT, not a cage: a person still switches spaces, and their explicit
choice wins over it" — and no place for that choice to be written down. So
clients kept inventing the rule, and the invention that shipped was
`workspaces[0]` off a list ordered by `-last_accessed_at`: the owner's four
pending invitations sat in the org space while his screen showed his
personal one (#239, measured on the meettoday stand 2026-08-06).

This is the missing half. The choice is STATED (PUT me/preferred-workspace),
never inferred from where somebody last clicked, and it is echoed back on the
list response the client already fetches so there is no window in which the
list has arrived but the answer has not.
"""
import uuid

import pytest

from stapel_workspaces.models import WorkspaceMember
from stapel_workspaces.services import create_workspace

LIST_URL = "/workspaces/api/workspaces/v1/"
URL = "/workspaces/api/workspaces/v1/me/preferred-workspace"


@pytest.mark.django_db
class TestPreferredWorkspace:
    def test_absent_by_default(self, authed_client, user):
        """A person who has never chosen gets "" — not a guess."""
        create_workspace(user=user, name="Personal")
        assert authed_client.get(LIST_URL).json()["preferred_workspace_id"] == ""

    def test_a_stated_choice_is_echoed_on_the_list(self, authed_client, user):
        create_workspace(user=user, name="Personal")
        org = create_workspace(user=user, name="Org")
        res = authed_client.put(
            URL, {"workspace_id": str(org.id)}, format="json"
        )
        assert res.status_code == 200
        assert res.json()["preferred_workspace_id"] == str(org.id)
        assert authed_client.get(LIST_URL).json()["preferred_workspace_id"] == str(
            org.id
        )

    def test_it_is_not_merely_the_first_row(self, authed_client, user):
        """The whole point: the answer is the choice, not list order."""
        first = create_workspace(user=user, name="First")
        second = create_workspace(user=user, name="Second")
        authed_client.put(URL, {"workspace_id": str(second.id)}, format="json")
        body = authed_client.get(LIST_URL).json()
        assert body["preferred_workspace_id"] == str(second.id)
        assert {w["id"] for w in body["workspaces"]} == {
            str(first.id),
            str(second.id),
        }

    def test_switching_replaces_rather_than_accumulates(self, authed_client, user):
        """At most one home per person — enforced by the partial unique index,
        so a second write cannot quietly leave two flags set."""
        a = create_workspace(user=user, name="A")
        b = create_workspace(user=user, name="B")
        authed_client.put(URL, {"workspace_id": str(a.id)}, format="json")
        authed_client.put(URL, {"workspace_id": str(b.id)}, format="json")
        flagged = WorkspaceMember.objects.filter(user=user, is_preferred=True)
        assert [str(m.workspace_id) for m in flagged] == [str(b.id)]

    def test_choosing_the_same_workspace_twice_is_idempotent(
        self, authed_client, user
    ):
        ws = create_workspace(user=user, name="Personal")
        authed_client.put(URL, {"workspace_id": str(ws.id)}, format="json")
        res = authed_client.put(URL, {"workspace_id": str(ws.id)}, format="json")
        assert res.status_code == 200
        assert WorkspaceMember.objects.filter(user=user, is_preferred=True).count() == 1

    def test_delete_clears_it(self, authed_client, user):
        ws = create_workspace(user=user, name="Personal")
        authed_client.put(URL, {"workspace_id": str(ws.id)}, format="json")
        res = authed_client.delete(URL)
        assert res.status_code == 200
        assert res.json()["preferred_workspace_id"] == ""
        assert authed_client.get(LIST_URL).json()["preferred_workspace_id"] == ""

    def test_delete_with_nothing_set_is_idempotent(self, authed_client, user):
        create_workspace(user=user, name="Personal")
        assert authed_client.delete(URL).status_code == 200

    def test_a_workspace_the_caller_is_not_in_is_refused(
        self, authed_client, user, other_user
    ):
        """Recording a pointer at a workspace the person cannot open would
        trade one wrong screen for another — the exact failure the axis
        exists to remove."""
        create_workspace(user=user, name="Mine")
        theirs = create_workspace(user=other_user, name="Theirs")
        res = authed_client.put(
            URL, {"workspace_id": str(theirs.id)}, format="json"
        )
        assert res.status_code == 404
        assert res.json()["localizable_error"] == "error.404.workspace_not_found"
        assert not WorkspaceMember.objects.filter(user=user, is_preferred=True).exists()

    def test_a_workspace_that_does_not_exist_answers_the_same_404(
        self, authed_client, user
    ):
        """Same key, same status — a caller must not be able to probe which
        workspace ids are real by watching the error change."""
        create_workspace(user=user, name="Mine")
        res = authed_client.put(URL, {"workspace_id": str(uuid.uuid4())}, format="json")
        assert res.status_code == 404
        assert res.json()["localizable_error"] == "error.404.workspace_not_found"

    def test_a_deleted_workspace_is_refused(self, authed_client, user):
        from django.utils import timezone

        ws = create_workspace(user=user, name="Doomed")
        ws.deleted_at = timezone.now()
        ws.save(update_fields=["deleted_at"])
        assert (
            authed_client.put(
                URL, {"workspace_id": str(ws.id)}, format="json"
            ).status_code
            == 404
        )

    def test_a_suspended_membership_cannot_be_chosen(self, authed_client, user):
        """Suspension closes the workspace to the member entirely (§C3); it
        must not become the place they are sent on next login."""
        from django.utils import timezone

        ws = create_workspace(user=user, name="Org")
        WorkspaceMember.objects.filter(user=user, workspace=ws).update(
            suspended_at=timezone.now(), suspension_reason="no_mfa"
        )
        assert (
            authed_client.put(
                URL, {"workspace_id": str(ws.id)}, format="json"
            ).status_code
            == 404
        )

    def test_a_suspension_silences_an_existing_choice_and_returns_it_after(
        self, authed_client, user
    ):
        """The flag survives the lifecycle; the ECHO is what goes quiet. A
        preference is not destroyed by a reversible state — otherwise lifting
        a suspension would silently forget where the person lived."""
        from django.utils import timezone

        create_workspace(user=user, name="Personal")
        org = create_workspace(user=user, name="Org")
        authed_client.put(URL, {"workspace_id": str(org.id)}, format="json")

        members = WorkspaceMember.objects.filter(user=user, workspace=org)
        members.update(suspended_at=timezone.now(), suspension_reason="no_mfa")
        assert authed_client.get(LIST_URL).json()["preferred_workspace_id"] == ""
        assert members.get().is_preferred is True

        members.update(suspended_at=None, suspension_reason="")
        assert authed_client.get(LIST_URL).json()["preferred_workspace_id"] == str(
            org.id
        )

    def test_leaving_the_workspace_takes_the_preference_with_it(
        self, authed_client, user
    ):
        """Self-healing by construction: no cleanup job keeps this true, the
        cascade does."""
        create_workspace(user=user, name="Personal")
        org = create_workspace(user=user, name="Org")
        authed_client.put(URL, {"workspace_id": str(org.id)}, format="json")
        WorkspaceMember.objects.filter(user=user, workspace=org).delete()
        assert authed_client.get(LIST_URL).json()["preferred_workspace_id"] == ""

    def test_uuid_and_string_actually_compare(self, authed_client, user):
        """Regression pin, same trap `default_workspace_id` documented: the
        column is a UUID and the wire value is a string, and
        `UUID(...) == "a8bb..."` is False in Python."""
        ws = create_workspace(user=user, name="Personal")
        assert ws.id != str(ws.id)  # the trap itself, stated
        authed_client.put(URL, {"workspace_id": str(ws.id)}, format="json")
        assert authed_client.get(LIST_URL).json()["preferred_workspace_id"] == str(
            ws.id
        )

    def test_the_preference_is_per_person(self, api_client, user, other_user):
        """Two people, two answers — the flag is scoped to the membership,
        never to the workspace."""
        shared = create_workspace(user=user, name="Shared")
        mine = create_workspace(user=user, name="Mine")
        theirs = create_workspace(user=other_user, name="Theirs")

        api_client.force_authenticate(user=user)
        api_client.put(URL, {"workspace_id": str(mine.id)}, format="json")
        api_client.force_authenticate(user=other_user)
        api_client.put(URL, {"workspace_id": str(theirs.id)}, format="json")

        api_client.force_authenticate(user=user)
        assert api_client.get(LIST_URL).json()["preferred_workspace_id"] == str(mine.id)
        api_client.force_authenticate(user=other_user)
        assert api_client.get(LIST_URL).json()["preferred_workspace_id"] == str(
            theirs.id
        )
        assert shared.id != mine.id

    def test_a_malformed_id_is_a_400_not_a_500(self, authed_client, user):
        create_workspace(user=user, name="Personal")
        assert authed_client.put(URL, {"workspace_id": "nope"}, format="json").status_code == 400

    def test_the_instance_default_is_still_reported_alongside(
        self, authed_client, user, settings
    ):
        """Both keys ride the same response; the CLIENT applies the order
        (choice first, instance default second). The server states facts and
        does not collapse them into one field — a client that only knows the
        instance default must keep working."""
        personal = create_workspace(user=user, name="Personal")
        org = create_workspace(user=user, name="Org")
        settings.STAPEL_WORKSPACES = {"DEFAULT_WORKSPACE_ID": str(org.id)}
        authed_client.put(URL, {"workspace_id": str(personal.id)}, format="json")
        body = authed_client.get(LIST_URL).json()
        assert body["default_workspace_id"] == str(org.id)
        assert body["preferred_workspace_id"] == str(personal.id)


@pytest.mark.django_db
class TestPreferredWorkspaceGuestSurface:
    def test_a_guest_gets_the_same_404(self, api_client, django_user_model):
        """A guest holds no membership anywhere, so there is nothing to
        prefer — and the answer must not tell it whether the id was real."""
        guest = django_user_model.create_anonymous_user()
        api_client.force_authenticate(user=guest)
        res = api_client.put(URL, {"workspace_id": str(uuid.uuid4())}, format="json")
        assert res.status_code == 404

    def test_a_guest_list_carries_an_empty_preference(
        self, api_client, django_user_model
    ):
        guest = django_user_model.create_anonymous_user()
        api_client.force_authenticate(user=guest)
        assert api_client.get(LIST_URL).json()["preferred_workspace_id"] == ""
