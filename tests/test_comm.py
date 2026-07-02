"""Tests for the comm surface of stapel-workspaces.

Covers:
- the ``workspaces.check_membership`` Function provider (in-process call),
- the actions emitted by the service layer (payloads validated against the
  committed JSON Schema contracts in schemas/emits/),
- cross-service cache invalidation and the ``workspace_member_changed``
  signal on the member API views.
"""

import json
from pathlib import Path

import jsonschema
import pytest
from django.core.cache import cache
from django.utils import timezone

from stapel_core.comm import call, subscribe_action
from stapel_core.django.workspaces import _cache_key
from stapel_core.signals import workspace_member_changed

import stapel_workspaces
from stapel_workspaces.models import Role, WorkspaceMember

SCHEMAS_DIR = Path(stapel_workspaces.__file__).resolve().parent / "schemas" / "emits"


def _load_schema(name: str) -> dict:
    return json.loads((SCHEMAS_DIR / f"{name}.json").read_text())


def _validate(payload: dict, event_name: str) -> None:
    """Validate a real emitted payload against the committed contract."""
    jsonschema.validate(
        payload,
        _load_schema(event_name),
        format_checker=jsonschema.FormatChecker(),
    )


@pytest.fixture
def capture():
    """Subscribe a collector to an action name; returns name -> list."""

    def _capture(name):
        events = []
        subscribe_action(name, events.append)
        return events

    return _capture


@pytest.mark.django_db
class TestCheckMembershipFunction:
    def test_member_returns_role(self, user):
        from stapel_workspaces.services import create_workspace

        ws = create_workspace(user=user, name="Acme")
        result = call(
            "workspaces.check_membership",
            {"workspace_id": str(ws.id), "user_id": str(user.pk)},
        )
        assert result == {"is_member": True, "role": "owner"}

    def test_non_member_returns_false_and_null_role(self, user, other_user):
        from stapel_workspaces.services import create_workspace

        ws = create_workspace(user=user, name="Acme")
        result = call(
            "workspaces.check_membership",
            {"workspace_id": str(ws.id), "user_id": str(other_user.pk)},
        )
        assert result == {"is_member": False, "role": None}

    def test_pending_membership_does_not_count(self, user, other_user):
        from stapel_workspaces.services import create_workspace

        ws = create_workspace(user=user, name="Acme")
        WorkspaceMember.objects.create(
            workspace=ws, user=other_user, role=Role.MEMBER, accepted_at=None
        )
        result = call(
            "workspaces.check_membership",
            {"workspace_id": str(ws.id), "user_id": str(other_user.pk)},
        )
        assert result == {"is_member": False, "role": None}


@pytest.mark.django_db
class TestEmittedEvents:
    def test_create_workspace_emits_workspace_created(self, user, capture):
        from stapel_workspaces.services import create_workspace

        events = capture("workspace.created")
        ws = create_workspace(user=user, name="Acme", type="work")
        assert len(events) == 1
        payload = events[0].payload
        assert payload == {
            "workspace_id": str(ws.id),
            "owner_id": str(user.pk),
            "name": "Acme",
            "type": "work",
        }
        _validate(payload, "workspace.created")

    def test_accept_invitation_emits_member_joined(self, user, other_user, capture):
        from stapel_workspaces.services import (
            accept_invitation,
            create_invitation,
            create_workspace,
        )

        ws = create_workspace(user=user, name="Acme")
        inv = create_invitation(
            workspace=ws, email=other_user.email, role=Role.MEMBER, invited_by=user
        )
        events = capture("workspace.member_joined")
        member = accept_invitation(invitation=inv, user=other_user)
        assert member.role == Role.MEMBER
        assert len(events) == 1
        payload = events[0].payload
        assert payload == {
            "workspace_id": str(ws.id),
            "user_id": str(other_user.pk),
            "role": "member",
        }
        _validate(payload, "workspace.member_joined")

    def test_personal_bootstrap_emits_personal_created_and_member_joined(
        self, user, capture
    ):
        from stapel_workspaces.services import ensure_personal_workspace

        personal = capture("workspace.personal.created")
        joined = capture("workspace.member_joined")
        ws = ensure_personal_workspace(user)

        assert len(personal) == 1
        assert personal[0].payload == {
            "workspace_id": str(ws.id),
            "user_id": str(user.pk),
        }
        _validate(personal[0].payload, "workspace.personal.created")

        assert len(joined) == 1
        assert joined[0].payload == {
            "workspace_id": str(ws.id),
            "user_id": str(user.pk),
            "role": "owner",
        }
        _validate(joined[0].payload, "workspace.member_joined")

        # Idempotent: a second call returns the same workspace, emits nothing.
        assert ensure_personal_workspace(user) == ws
        assert len(personal) == 1
        assert len(joined) == 1


@pytest.fixture
def signal_log():
    received = []

    def receiver(sender, **kwargs):
        received.append(kwargs)

    workspace_member_changed.connect(receiver)
    yield received
    workspace_member_changed.disconnect(receiver)


@pytest.mark.django_db
class TestMemberChangeInvalidation:
    def _workspace_with_member(self, owner, member_user):
        from stapel_workspaces.services import create_workspace

        ws = create_workspace(user=owner, name="Acme")
        WorkspaceMember.objects.create(
            workspace=ws, user=member_user, role=Role.MEMBER, accepted_at=timezone.now()
        )
        return ws

    def test_role_change_invalidates_cache_and_signals(
        self, authed_client, user, other_user, signal_log
    ):
        ws = self._workspace_with_member(user, other_user)
        key = _cache_key(ws.id, other_user.pk)
        cache.set(key, "member", 30)

        resp = authed_client.patch(
            f"/workspaces/api/workspaces/{ws.id}/members/{other_user.pk}",
            {"role": "admin"},
            format="json",
        )
        assert resp.status_code == 200, resp.content
        assert cache.get(key) is None
        updates = [s for s in signal_log if s["action"] == "updated"]
        assert len(updates) == 1
        assert updates[0]["workspace"].id == ws.id
        assert updates[0]["user"] == other_user
        assert updates[0]["role"] == "admin"

    def test_member_removal_invalidates_cache_and_signals(
        self, authed_client, user, other_user, signal_log
    ):
        ws = self._workspace_with_member(user, other_user)
        key = _cache_key(ws.id, other_user.pk)
        cache.set(key, "member", 30)

        resp = authed_client.delete(
            f"/workspaces/api/workspaces/{ws.id}/members/{other_user.pk}"
        )
        assert resp.status_code == 204, resp.content
        assert cache.get(key) is None
        assert not WorkspaceMember.objects.filter(
            workspace=ws, user=other_user
        ).exists()
        removals = [s for s in signal_log if s["action"] == "removed"]
        assert len(removals) == 1
        assert removals[0]["workspace"].id == ws.id
        assert removals[0]["user"] == other_user
        assert removals[0]["role"] == "member"

    def test_accept_invitation_invalidates_cache_and_signals_added(
        self, user, other_user, signal_log
    ):
        from stapel_workspaces.services import (
            accept_invitation,
            create_invitation,
            create_workspace,
        )

        ws = create_workspace(user=user, name="Acme")
        inv = create_invitation(
            workspace=ws, email=other_user.email, role=Role.VIEWER, invited_by=user
        )
        key = _cache_key(ws.id, other_user.pk)
        cache.set(key, "__none__", 30)  # cached negative lookup

        accept_invitation(invitation=inv, user=other_user)
        assert cache.get(key) is None
        added = [s for s in signal_log if s["action"] == "added" and s["user"] == other_user]
        assert len(added) == 1
        assert added[0]["role"] == "viewer"


@pytest.mark.django_db
class TestInvitationNotification:
    def test_create_invitation_requests_notification(self, user, other_user, monkeypatch):
        from stapel_workspaces import services

        sent = []

        def fake_request_notification(notification_type, **kwargs):
            sent.append((notification_type, kwargs))
            return True

        monkeypatch.setattr(
            "stapel_core.notifications.request_notification",
            fake_request_notification,
        )
        ws = services.create_workspace(user=user, name="Acme")
        inv = services.create_invitation(
            workspace=ws, email=other_user.email, role=Role.MEMBER, invited_by=user
        )

        assert len(sent) == 1
        notification_type, kwargs = sent[0]
        assert notification_type == "workspace.invitation"
        # Invitee is registered -> targeted by user_id.
        assert kwargs["user_id"] == str(other_user.pk)
        assert kwargs["variables"]["workspace_name"] == "Acme"
        assert kwargs["variables"]["inviter_name"]
        assert (
            kwargs["variables"]["accept_url"]
            == f"https://app.example.com/invitations/{inv.token}/accept"
        )

    def test_unregistered_invitee_targeted_by_email(self, user, monkeypatch):
        from stapel_workspaces import services

        sent = []
        monkeypatch.setattr(
            "stapel_core.notifications.request_notification",
            lambda notification_type, **kwargs: sent.append(kwargs) or True,
        )
        ws = services.create_workspace(user=user, name="Acme")
        services.create_invitation(
            workspace=ws, email="stranger@example.com", role=Role.MEMBER, invited_by=user
        )
        assert len(sent) == 1
        assert sent[0].get("user_id") is None
        assert sent[0]["email"] == "stranger@example.com"

    def test_notification_failure_does_not_break_invitation(self, user, monkeypatch):
        from stapel_workspaces import services

        def boom(*args, **kwargs):
            raise RuntimeError("bus down")

        monkeypatch.setattr("stapel_core.notifications.request_notification", boom)
        ws = services.create_workspace(user=user, name="Acme")
        inv = services.create_invitation(
            workspace=ws, email="x@example.com", role=Role.MEMBER, invited_by=user
        )
        assert inv.pk is not None
