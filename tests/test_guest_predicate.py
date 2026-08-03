"""``permissions.is_guest`` / ``has_active_mandate`` (org-program #85/#87, vardict 2026-08-03).

Developer decision #1 of the mandate-model vardict: guest is a STATE
("authenticated, no active mandate anywhere"), not a role — this predicate
is the library-level primitive so product backend code stops hand-rolling
"earliest accepted membership is None" per-project, and so the *wire* form
(``WorkspaceListResponse.is_guest``) has one shared source of truth instead
of silently drifting from it.
"""
import pytest
from django.utils import timezone

from stapel_workspaces.models import Role, WorkspaceMember
from stapel_workspaces.permissions import has_active_mandate, is_guest
from stapel_workspaces.services import create_workspace

BASE = "/workspaces/api/workspaces/v1"


@pytest.mark.django_db
class TestHasActiveMandate:
    def test_anonymous_session_has_none(self, db):
        from stapel_core.django.users.models import User

        anon = User.create_anonymous_user()
        assert has_active_mandate(anon) is False

    def test_fresh_user_with_no_membership_has_none(self, user):
        assert has_active_mandate(user) is False

    def test_owner_of_own_workspace_has_one(self, user):
        create_workspace(user=user, name="Acme")
        assert has_active_mandate(user) is True

    def test_pending_invitation_does_not_count(self, user, other_user):
        ws = create_workspace(user=user, name="Acme")
        WorkspaceMember.objects.create(workspace=ws, user=other_user, role=Role.MEMBER)
        assert has_active_mandate(other_user) is False

    def test_suspended_membership_does_not_count(self, user, other_user):
        ws = create_workspace(user=user, name="Acme")
        m = WorkspaceMember.objects.create(
            workspace=ws, user=other_user, role=Role.MEMBER,
            accepted_at=timezone.now(),
        )
        m.suspended_at = timezone.now()
        m.suspension_reason = "no_mfa"
        m.save(update_fields=["suspended_at", "suspension_reason"])
        assert has_active_mandate(other_user) is False

    def test_membership_in_a_soft_deleted_workspace_does_not_count(self, user):
        ws = create_workspace(user=user, name="Acme")
        ws.deleted_at = timezone.now()
        ws.save(update_fields=["deleted_at"])
        assert has_active_mandate(user) is False

    def test_member_of_a_becomes_guest_of_b(self, user, other_user):
        """The relational case the vardict rejected a role for: membership
        in ONE workspace does not make you a mandate-holder everywhere."""
        create_workspace(user=other_user, name="Their org")
        # `user` owns nothing and belongs to nothing of `other_user`'s.
        assert has_active_mandate(user) is False


@pytest.mark.django_db
class TestIsGuest:
    def test_is_the_negation(self, user):
        assert is_guest(user) is (not has_active_mandate(user))
        create_workspace(user=user, name="Acme")
        assert is_guest(user) is (not has_active_mandate(user))

    def test_anonymous_is_a_guest(self, db):
        from stapel_core.django.users.models import User

        anon = User.create_anonymous_user()
        assert is_guest(anon) is True

    def test_registered_without_mandate_is_also_a_guest(self, user):
        """The owner's "same as Anonymous" decision: a registered account
        with no membership anywhere gets the identical answer."""
        assert is_guest(user) is True

    def test_member_stops_being_a_guest(self, user):
        create_workspace(user=user, name="Acme")
        assert is_guest(user) is False


@pytest.mark.django_db
class TestWireExposure:
    """``GET /`` — the same predicate, over the wire (workspaces-react's
    live guest path)."""

    def test_guest_field_true_for_a_registered_mandate_less_user(self, authed_client, user):
        resp = authed_client.get(f"{BASE}/")
        assert resp.status_code == 200, resp.content
        assert resp.json() == {"workspaces": [], "is_guest": True}
        assert is_guest(user) is True

    def test_guest_field_false_once_a_membership_exists(self, authed_client, user):
        create_workspace(user=user, name="Acme")
        resp = authed_client.get(f"{BASE}/")
        assert resp.json()["is_guest"] is False
        assert is_guest(user) is False

    def test_guest_field_never_drifts_from_the_predicate(
        self, api_client, user, other_user
    ):
        """Direct regression pin for the endpoint's own comment: the wire
        field must equal permissions.is_guest(user) in every fixture below,
        not merely "empty workspaces list"."""
        ws = create_workspace(user=other_user, name="Acme")
        for accepted, expected_guest in [(False, True), (True, False)]:
            WorkspaceMember.objects.filter(workspace=ws, user=user).delete()
            WorkspaceMember.objects.create(
                workspace=ws,
                user=user,
                role=Role.VIEWER,
                accepted_at=timezone.now() if accepted else None,
            )
            api_client.force_authenticate(user=user)
            resp = api_client.get(f"{BASE}/")
            assert resp.json()["is_guest"] == expected_guest == is_guest(user)
