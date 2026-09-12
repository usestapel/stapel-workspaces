"""``consume_auth_events`` when the shadow user row is not there yet.

The bundled consumer turns ``user.registered`` into the account's landing
workspace. It used to need a LOCAL user row to do it, and in a microservices
deployment that row is not written by this event at all — it is written by
the JWT seam on the account's first authenticated request to this service.

So the two writers race, and the consumer normally wins: the event is
published inside the registration request, and the client's first call to
this service comes after. The consumer then wrote "user <id> not found,
skipping" and committed the offset, so nothing ever replayed it and the
account had no workspace for the rest of its life — measured on a deployed
stand 2026-09-12, where an anonymous enroll (``user.registered`` at
12:58:52.55, shadow row at 12:58:53.77) left the guest with no workspace and
every workspace-scoped call after it dead.

The event is the issuer speaking about an account it has just created, so it
can materialise the row itself — in consumer (shadow-copy) mode. In
authoritative mode the local database decides who exists and the skip is
still the right answer; both are asserted here.
"""
import uuid
from io import StringIO

import pytest
from django.test import override_settings

from stapel_workspaces.models import Workspace, WorkspaceMember, WorkspaceType


def _command():
    from stapel_workspaces.management.commands.consume_auth_events import Command

    return Command(stdout=StringIO(), stderr=StringIO())


def _event(payload, event_type="user.registered"):
    from stapel_core.bus import Event

    return Event(event_type=event_type, service="auth", payload=payload)


def _registered(user_id, **extra):
    payload = {
        "user_id": str(user_id),
        "auth_type": "anonymous",
        "email": None,
        "avatar_url": None,
        "language": None,
        "display_name": None,
        "is_anonymous": True,
    }
    payload.update(extra)
    return payload


@pytest.fixture
def consumer_mode():
    """Shadow-copy mode — what every downstream service in a fleet runs."""
    with override_settings(JWT_CREATE_USERS_FROM_TOKEN=True):
        yield


@pytest.mark.django_db
@pytest.mark.usefixtures("consumer_mode")
class TestConsumerModeMaterialisesTheRow:
    """Shadow-copy mode: the event is enough, no prior request needed."""

    def test_anonymous_enroll_gets_its_workspace(self):
        uid = uuid.uuid4()
        cmd = _command()
        cmd.handle_event(_event(_registered(uid)))

        from django.contrib.auth import get_user_model

        user = get_user_model().objects.get(pk=uid)
        assert user.is_anonymous is True
        assert user.auth_type == "anonymous"
        ws = Workspace.objects.get(owner_id=user.pk, type=WorkspaceType.PERSONAL)
        assert WorkspaceMember.objects.filter(workspace=ws, user=user).exists()
        assert "Bootstrapped personal workspace" in cmd.stdout.getvalue()

    def test_password_signup_gets_its_workspace(self):
        uid = uuid.uuid4()
        cmd = _command()
        cmd.handle_event(_event(_registered(
            uid, auth_type="email", is_anonymous=False,
            email="new@example.com", display_name="New",
        )))

        from django.contrib.auth import get_user_model

        user = get_user_model().objects.get(pk=uid)
        assert user.email == "new@example.com"
        assert user.is_anonymous is False
        assert Workspace.objects.filter(
            owner_id=user.pk, type=WorkspaceType.PERSONAL
        ).exists()

    def test_no_privileges_are_taken_from_the_payload(self):
        """A bus payload may not mint a local superuser, whatever it says."""
        uid = uuid.uuid4()
        _command().handle_event(_event(_registered(
            uid, is_staff=True, is_superuser=True,
        )))

        from django.contrib.auth import get_user_model

        user = get_user_model().objects.get(pk=uid)
        assert user.is_staff is False
        assert user.is_superuser is False

    def test_existing_row_is_reused_not_duplicated(self, user):
        cmd = _command()
        cmd.handle_event(_event(_registered(
            user.pk, auth_type="email", is_anonymous=False, email=user.email,
        )))
        assert Workspace.objects.filter(owner=user).count() == 1

        cmd.handle_event(_event(_registered(
            user.pk, auth_type="email", is_anonymous=False, email=user.email,
        )))
        assert Workspace.objects.filter(owner=user).count() == 1

        from django.contrib.auth import get_user_model

        assert get_user_model().objects.filter(pk=user.pk).count() == 1

    def test_landing_mode_none_still_creates_no_workspace(self):
        """Materialising the row must not slip past the landing axis."""
        uid = uuid.uuid4()
        with override_settings(STAPEL_WORKSPACES={"STREET_LANDING_MODE": "none"}):
            cmd = _command()
            cmd.handle_event(_event(_registered(uid)))
        assert Workspace.objects.count() == 0
        assert "no workspace created" in cmd.stdout.getvalue()


@pytest.mark.django_db
class TestAuthoritativeModeStillSkips:
    """Default mode: the local database decides who exists."""

    def test_unknown_user_is_skipped_and_not_created(self):
        uid = uuid.uuid4()
        cmd = _command()
        cmd.handle_event(_event(_registered(uid)))

        from django.contrib.auth import get_user_model

        assert not get_user_model().objects.filter(pk=uid).exists()
        assert Workspace.objects.count() == 0
        assert "not found, skipping" in cmd.stderr.getvalue()
