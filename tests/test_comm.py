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
        assert result == {"is_member": True, "role": "owner", "capabilities": ["*"]}

    def test_non_member_returns_false_and_null_role(self, user, other_user):
        from stapel_workspaces.services import create_workspace

        ws = create_workspace(user=user, name="Acme")
        result = call(
            "workspaces.check_membership",
            {"workspace_id": str(ws.id), "user_id": str(other_user.pk)},
        )
        assert result == {"is_member": False, "role": None, "capabilities": []}

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
        assert result == {"is_member": False, "role": None, "capabilities": []}


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
            f"/workspaces/api/workspaces/v1/{ws.id}/members/{other_user.pk}",
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
            f"/workspaces/api/workspaces/v1/{ws.id}/members/{other_user.pk}"
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
        # Canonical frontend invite route (org-program spec §B1, Wave 2).
        assert (
            kwargs["variables"]["accept_url"]
            == f"https://app.example.com/invite/{inv.token}"
        )

    def test_registered_invitee_also_carries_email(
        self, user, other_user, monkeypatch
    ):
        """A known invitee must be reachable even with no UserContact row.

        stapel-notifications resolves the recipient address from its own
        UserContact table ONLY when the request omits ``email`` — an
        explicit ``email`` overrides that lookup
        (``recipient_email = email or (contact.email if contact else
        None)``). Before this test's fix, a registered invitee was
        targeted by ``user_id`` alone: an account that predates
        UserContact, or was never enriched into it, has no row there and
        the invite produced zero deliverable channels (created 201, then
        silently "no email address for this recipient" in the log).
        Found on the meettoday sandbox 2026-08 by inviting a pre-existing
        account. Carrying ``email`` alongside ``user_id`` always closes the
        gap regardless of what UserContact does or doesn't have.
        """
        from stapel_workspaces import services

        sent = []
        monkeypatch.setattr(
            "stapel_core.notifications.request_notification",
            lambda notification_type, **kwargs: sent.append(kwargs) or True,
        )
        ws = services.create_workspace(user=user, name="Acme")
        services.create_invitation(
            workspace=ws, email=other_user.email, role=Role.MEMBER, invited_by=user
        )
        assert len(sent) == 1
        assert sent[0]["user_id"] == str(other_user.pk)
        assert sent[0]["email"] == other_user.email

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

    def test_inviter_name_prefers_the_profile_over_the_generated_login(
        self, user, other_user, settings, monkeypatch
    ):
        """The letter must never show the inviter's generated username.

        ``user`` (the conftest fixture) has no first/last name — its
        ``get_full_name()`` is empty — and a generated ``u-xxxxxxxx``
        username. Before this test's fix, the fallback chain was
        ``get_full_name() -> username -> email``, so that generated login
        landed in the invitee's inbox as the inviter's name (owner report:
        "came out janky, some kind of generated username"). The canonical
        name lives in stapel-profiles (0.16.0's best-effort batch read,
        reused here rather than duplicated) and must win when present.
        """
        from stapel_workspaces import services

        settings.PROFILES_SERVICE_URL = "http://stapel-profiles:8000"

        class _Resp:
            status_code = 200
            headers = {"Content-Type": "application/json"}

            def json(self):
                return {
                    "profiles": [
                        {"user_id": str(user.pk), "display_name": "Ada Lovelace"}
                    ],
                    "missing": [],
                }

        monkeypatch.setattr(services.requests, "post", lambda *a, **k: _Resp())

        sent = []
        monkeypatch.setattr(
            "stapel_core.notifications.request_notification",
            lambda notification_type, **kwargs: sent.append(kwargs) or True,
        )
        ws = services.create_workspace(user=user, name="Acme")
        services.create_invitation(
            workspace=ws, email=other_user.email, role=Role.MEMBER, invited_by=user
        )
        assert sent[0]["variables"]["inviter_name"] == "Ada Lovelace"

    def test_inviter_name_falls_back_to_email_not_the_generated_login(
        self, user, other_user, settings, monkeypatch
    ):
        """No profile, no full name -> the email, never ``username``."""
        from stapel_workspaces import services

        settings.PROFILES_SERVICE_URL = ""  # integration off -> {} every time

        sent = []
        monkeypatch.setattr(
            "stapel_core.notifications.request_notification",
            lambda notification_type, **kwargs: sent.append(kwargs) or True,
        )
        ws = services.create_workspace(user=user, name="Acme")
        services.create_invitation(
            workspace=ws, email=other_user.email, role=Role.MEMBER, invited_by=user
        )
        assert sent[0]["variables"]["inviter_name"] == user.email
        assert sent[0]["variables"]["inviter_name"] != user.username

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


@pytest.mark.django_db
class TestCheckCapabilityFunction:
    def _ws(self, owner):
        from stapel_workspaces.services import create_workspace

        return create_workspace(user=owner, name="Acme")

    def test_owner_allowed_via_wildcard(self, user):
        ws = self._ws(user)
        result = call(
            "workspaces.check_capability",
            {
                "workspace_id": str(ws.id),
                "user_id": str(user.pk),
                "capability": "meetings.kick",
            },
        )
        assert result == {"allowed": True, "role": "owner"}

    def test_member_without_capability_denied_but_role_reported(
        self, user, other_user
    ):
        ws = self._ws(user)
        WorkspaceMember.objects.create(
            workspace=ws, user=other_user, role=Role.MEMBER,
            accepted_at=timezone.now(),
        )
        result = call(
            "workspaces.check_capability",
            {
                "workspace_id": str(ws.id),
                "user_id": str(other_user.pk),
                "capability": "members.invite",
            },
        )
        assert result == {"allowed": False, "role": "member"}

    def test_non_member_denied_null_role(self, user, other_user):
        ws = self._ws(user)
        result = call(
            "workspaces.check_capability",
            {
                "workspace_id": str(ws.id),
                "user_id": str(other_user.pk),
                "capability": "workspace.view",
            },
        )
        assert result == {"allowed": False, "role": None}

    def test_custom_registry_role_resolved(self, user, other_user, settings):
        settings.STAPEL_WORKSPACES = {
            "ROLES": {
                "secretary": {
                    "rank": 250,
                    "capabilities": ["workspace.view", "meetings.spotlight"],
                },
            },
        }
        ws = self._ws(user)
        WorkspaceMember.objects.create(
            workspace=ws, user=other_user, role="secretary",
            accepted_at=timezone.now(),
        )
        result = call(
            "workspaces.check_capability",
            {
                "workspace_id": str(ws.id),
                "user_id": str(other_user.pk),
                "capability": "meetings.spotlight",
            },
        )
        assert result == {"allowed": True, "role": "secretary"}

    def test_check_membership_carries_role_capabilities(self, user, other_user):
        ws = self._ws(user)
        WorkspaceMember.objects.create(
            workspace=ws, user=other_user, role=Role.VIEWER,
            accepted_at=timezone.now(),
        )
        result = call(
            "workspaces.check_membership",
            {"workspace_id": str(ws.id), "user_id": str(other_user.pk)},
        )
        assert result == {
            "is_member": True,
            "role": "viewer",
            "capabilities": ["workspace.view", "members.view"],
        }


@pytest.mark.django_db
class TestMemberLifecycleEmits:
    """Outbox emits on kick / role change (org-program spec §A4)."""

    def _workspace_with_member(self, owner, member_user, role=Role.MEMBER):
        from stapel_workspaces.services import create_workspace

        ws = create_workspace(user=owner, name="Acme")
        WorkspaceMember.objects.create(
            workspace=ws, user=member_user, role=role, accepted_at=timezone.now()
        )
        return ws

    def test_role_change_emits_member_role_changed(
        self, authed_client, user, other_user, capture
    ):
        ws = self._workspace_with_member(user, other_user)
        events = capture("workspace.member_role_changed")
        resp = authed_client.patch(
            f"/workspaces/api/workspaces/v1/{ws.id}/members/{other_user.pk}",
            {"role": "admin"},
            format="json",
        )
        assert resp.status_code == 200, resp.content
        assert len(events) == 1
        payload = events[0].payload
        assert payload == {
            "workspace_id": str(ws.id),
            "user_id": str(other_user.pk),
            "old_role": "member",
            "new_role": "admin",
            "capabilities": [
                "workspace.view", "workspace.update",
                "members.view", "members.invite", "members.remove",
                "members.role.change", "members.provision",
                "members.password.reset",
                "workspace.security.manage",
            ],
        }
        _validate(payload, "workspace.member_role_changed")

    def test_remove_emits_member_removed(
        self, authed_client, user, other_user, capture
    ):
        ws = self._workspace_with_member(user, other_user)
        events = capture("workspace.member_removed")
        resp = authed_client.delete(
            f"/workspaces/api/workspaces/v1/{ws.id}/members/{other_user.pk}"
        )
        assert resp.status_code == 204, resp.content
        assert len(events) == 1
        payload = events[0].payload
        assert payload == {
            "workspace_id": str(ws.id),
            "user_id": str(other_user.pk),
            "role": "member",
            "removed_by": str(user.pk),
        }
        _validate(payload, "workspace.member_removed")

    def test_failed_role_change_emits_nothing(self, authed_client, user, capture):
        """Last-owner rejection must not leak an emit."""
        from stapel_workspaces.services import create_workspace

        ws = create_workspace(user=user, name="Solo")
        events = capture("workspace.member_role_changed")
        resp = authed_client.patch(
            f"/workspaces/api/workspaces/v1/{ws.id}/members/{user.pk}",
            {"role": "admin"},
            format="json",
        )
        assert resp.status_code == 403
        assert events == []
