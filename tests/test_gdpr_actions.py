"""Tests for the GDPR provider, action subscriptions, permissions helpers,
error registry, admin registrations and the auth-events consumer command."""

import types
import uuid
from io import StringIO

import pytest
from django.utils import timezone

from stapel_workspaces.actions import handle_user_deleted
from stapel_workspaces.errors import WORKSPACES_ERRORS, WorkspacesErrorKeysView
from stapel_workspaces.gdpr import WorkspacesGDPRProvider
from stapel_workspaces.models import (
    Role,
    Workspace,
    WorkspaceInvitation,
    WorkspaceMember,
    WorkspaceType,
)
from stapel_workspaces.permissions import role_at_least


def _create_ws(user, name="Acme"):
    from stapel_workspaces.services import create_workspace

    return create_workspace(user=user, name=name)


@pytest.mark.django_db
class TestGDPRExport:
    def test_export_contains_memberships_and_invitations(self, user, other_user):
        ws = _create_ws(user)
        from stapel_workspaces.services import create_invitation

        create_invitation(
            workspace=ws, email="x@example.com", role=Role.MEMBER, invited_by=user
        )

        data = WorkspacesGDPRProvider().export(user.id)

        assert len(data["memberships"]) == 1
        assert data["memberships"][0]["role"] == Role.OWNER
        assert len(data["owned_workspaces"]) == 1
        assert data["owned_workspaces"][0]["slug"] == ws.slug
        assert len(data["invitations_sent"]) == 1

    def test_export_empty_for_unknown_user(self, db):
        data = WorkspacesGDPRProvider().export(uuid.uuid4())
        assert data == {
            "memberships": [],
            "owned_workspaces": [],
            "invitations_sent": [],
        }


@pytest.mark.django_db
class TestGDPRDeleteAnonymize:
    def test_delete_removes_memberships_and_soft_deletes_owned(
        self, user, other_user
    ):
        ws = _create_ws(user)
        from stapel_workspaces.services import create_invitation

        create_invitation(
            workspace=ws, email="p@example.com", role=Role.MEMBER, invited_by=user
        )
        accepted = create_invitation(
            workspace=ws,
            email=other_user.email,
            role=Role.MEMBER,
            invited_by=user,
        )
        accepted.accepted_at = timezone.now()
        accepted.save(update_fields=["accepted_at"])

        WorkspacesGDPRProvider().delete(user.id)

        assert not WorkspaceMember.objects.filter(user=user).exists()
        # Pending invitation removed, accepted one kept
        assert WorkspaceInvitation.objects.count() == 1
        ws.refresh_from_db()
        assert ws.deleted_at is not None

    def test_anonymize_unlinks_inviter_on_accepted(self, user, other_user):
        ws = _create_ws(user)
        from stapel_workspaces.services import create_invitation

        inv = create_invitation(
            workspace=ws,
            email=other_user.email,
            role=Role.MEMBER,
            invited_by=user,
        )
        inv.accepted_at = timezone.now()
        inv.save(update_fields=["accepted_at"])

        WorkspacesGDPRProvider().anonymize(user.id)

        inv.refresh_from_db()
        assert inv.invited_by is None


@pytest.mark.django_db
class TestUserDeletedAction:
    def test_handler_erases_workspace_data(self, user):
        _create_ws(user)
        handle_user_deleted(
            types.SimpleNamespace(payload={"user_id": user.id}, event_id="e1")
        )
        assert not WorkspaceMember.objects.filter(user=user).exists()

    def test_handler_without_user_id_skips(self, user, caplog):
        _create_ws(user)
        handle_user_deleted(types.SimpleNamespace(payload={}, event_id="e2"))
        assert WorkspaceMember.objects.filter(user=user).exists()
        assert "without user_id" in caplog.text


class TestPermissionsAndErrors:
    def test_role_at_least_unknown_role_is_false(self):
        assert role_at_least("galactic-emperor", Role.VIEWER) is False
        assert role_at_least(Role.OWNER, "galactic-emperor") is False

    def test_error_keys_view_returns_service_errors(self):
        assert (
            WorkspacesErrorKeysView().get_service_errors() is WORKSPACES_ERRORS
        )


@pytest.mark.django_db
class TestModelStr:
    def test_str_representations(self, user):
        ws = _create_ws(user, name="Str WS")
        member = ws.members.get()
        assert str(ws) == "Str WS (work)"
        assert str(member) == f"{user.id} @ {ws.id} (owner)"


def test_admin_registrations():
    from django.contrib import admin as django_admin

    from stapel_workspaces import admin as ws_admin  # noqa: F401

    for model in (Workspace, WorkspaceMember, WorkspaceInvitation):
        assert model in django_admin.site._registry


@pytest.mark.django_db
class TestConsumeAuthEventsCommand:
    def _command(self):
        from stapel_workspaces.management.commands.consume_auth_events import (
            Command,
        )

        cmd = Command(stdout=StringIO(), stderr=StringIO())
        return cmd

    def _event(self, payload, event_type="user.registered"):
        from stapel_core.bus import Event

        return Event(event_type=event_type, service="auth", payload=payload)

    def test_user_registered_bootstraps_personal_workspace(self, user):
        cmd = self._command()
        cmd.handle_event(self._event({"user_id": str(user.id)}))
        assert Workspace.objects.filter(
            owner=user, type=WorkspaceType.PERSONAL
        ).exists()
        assert "Bootstrapped personal workspace" in cmd.stdout.getvalue()

    def test_missing_user_id_logged(self, db):
        cmd = self._command()
        cmd.handle_event(self._event({}))
        assert "missing user_id" in cmd.stderr.getvalue()
        assert Workspace.objects.count() == 0

    def test_unknown_user_skipped(self, db):
        cmd = self._command()
        cmd.handle_event(self._event({"user_id": str(uuid.uuid4())}))
        assert "not found, skipping" in cmd.stderr.getvalue()
        assert Workspace.objects.count() == 0

    def test_other_event_types_ignored(self, db):
        cmd = self._command()
        cmd.handle_event(self._event({"user_id": "x"}, event_type="user.updated"))
        assert Workspace.objects.count() == 0
