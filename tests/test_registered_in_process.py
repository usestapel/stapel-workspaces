"""``user.registered`` lands a fresh account on the in-process bus too.

The personal-workspace bootstrap used to live only in the
``consume_auth_events`` broker command. On the in-process bus (the default,
and every monolith) nothing runs that command, so a self-registered account
had no workspace and could not create its first project. The ``@on_action``
twin shares one body with the command, and both together still make one
workspace and one ``workspace.personal.created``.
"""
from io import StringIO

import pytest
from django.test import override_settings

from stapel_core.bus import Event
from stapel_core.comm import emit, subscribe_action

from stapel_workspaces.models import Workspace, WorkspaceMember, WorkspaceType

def _registered(user):
    return {
        "user_id": str(user.pk),
        "auth_type": "email",
        "email": user.email,
        "is_anonymous": False,
    }


def _personal(user):
    return Workspace.objects.filter(
        owner=user, type=WorkspaceType.PERSONAL, deleted_at__isnull=True
    )


@pytest.mark.django_db
class TestInProcessSubscriber:
    def test_registration_emit_creates_the_personal_workspace(self, user):
        emit("user.registered", _registered(user))

        ws = _personal(user).get()
        assert WorkspaceMember.objects.filter(workspace=ws, user=user).exists()

    def test_subscriber_and_broker_command_make_one_workspace_one_event(self, user):
        from stapel_workspaces.management.commands.consume_auth_events import Command

        seen = []
        subscribe_action(
            "workspace.personal.created",
            lambda e: seen.append(e) if e.payload.get("user_id") == str(user.pk) else None,
        )
        emit("user.registered", _registered(user))
        Command(stdout=StringIO(), stderr=StringIO()).handle_event(
            Event(event_type="user.registered", service="auth", payload=_registered(user))
        )
        emit("user.registered", _registered(user))  # redelivery

        assert _personal(user).count() == 1
        assert len(seen) == 1
        assert seen[0].payload["email"] == user.email
        assert seen[0].payload["is_anonymous"] is False

    @override_settings(STAPEL_WORKSPACES={"LAND_ON_REGISTERED": False})
    def test_switch_off_leaves_the_registration_to_the_project(self, user):
        emit("user.registered", _registered(user))
        assert not _personal(user).exists()

    def test_closed_landing_mode_creates_nothing(self, user):
        with override_settings(STAPEL_WORKSPACES={"STREET_LANDING_MODE": "none"}):
            emit("user.registered", _registered(user))
        assert not _personal(user).exists()
