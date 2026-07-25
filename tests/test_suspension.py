"""Suspension mechanics + require_mfa policy (org-program spec §C3, Wave 3).

Suspension is not removal: the row stays, but every access surface —
permission helpers, comm functions, the internal HTTP check, the API views
— stops counting the membership until the suspension lifts. The auth side
(``auth.mfa_status`` and the ``user.mfa_enabled|disabled`` events) is faked
via the comm function registry / direct handler calls.
"""

import json
import types
from pathlib import Path

import jsonschema
import pytest
from django.core.cache import cache
from django.utils import timezone

from stapel_core.comm import call, subscribe_action
from stapel_core.comm.registry import function_registry
from stapel_core.django.workspaces import _cache_key
from stapel_core.verification import grant_verification

import stapel_workspaces
from stapel_workspaces import services
from stapel_workspaces.actions import (
    handle_user_mfa_disabled,
    handle_user_mfa_enabled,
)
from stapel_workspaces.errors import (
    ERR_403_MEMBERSHIP_SUSPENDED,
    ERR_403_MISSING_CAPABILITY,
)
from stapel_workspaces.models import SUSPENSION_NO_MFA, Role, WorkspaceMember
from stapel_workspaces.permissions import (
    get_membership,
    has_capability,
    require_capability,
)

BASE = "/workspaces/api/workspaces/v1"
SCHEMAS_DIR = Path(stapel_workspaces.__file__).resolve().parent / "schemas" / "emits"


def _validate(payload, event_name):
    jsonschema.validate(
        payload,
        json.loads((SCHEMAS_DIR / f"{event_name}.json").read_text()),
        format_checker=jsonschema.FormatChecker(),
    )


def _ws(user, name="Acme"):
    return services.create_workspace(user=user, name=name)


def _member(ws, user, role=Role.MEMBER, **kwargs):
    return WorkspaceMember.objects.create(
        workspace=ws, user=user, role=role, accepted_at=timezone.now(), **kwargs
    )


@pytest.fixture
def capture():
    def _capture(name):
        events = []
        subscribe_action(name, events.append)
        return events

    return _capture


@pytest.fixture
def sent_notifications(monkeypatch):
    sent = []
    monkeypatch.setattr(
        "stapel_core.notifications.request_notification",
        lambda notification_type, **kwargs: sent.append(
            (notification_type, kwargs)
        )
        or True,
    )
    return sent


@pytest.fixture
def fake_mfa_status():
    """Scripted ``auth.mfa_status`` provider.

    ``strong`` is a set of user pks (as str) that report has_strong_mfa.
    """
    state = {"calls": [], "strong": set()}

    def provider(payload):
        state["calls"].append(payload)
        has_strong = payload["user_id"] in state["strong"]
        return {
            "has_strong_mfa": has_strong,
            "factors": [{"id": "totp", "strength": "strong"}] if has_strong else [],
        }

    function_registry.register(services.MFA_STATUS, provider)
    yield state
    function_registry._providers.pop(services.MFA_STATUS, None)
    function_registry._schemas.pop(services.MFA_STATUS, None)


@pytest.mark.django_db
class TestSuspendedMembershipDoesNotCount:
    def test_permission_helpers_filter_suspended(self, user, other_user):
        ws = _ws(user)
        member = _member(ws, other_user)
        assert has_capability(ws.id, other_user.id, "workspace.view")

        services.suspend_member(member, reason=SUSPENSION_NO_MFA)
        assert get_membership(ws.id, other_user.id) is None
        assert not has_capability(ws.id, other_user.id, "workspace.view")
        assert require_capability(ws.id, other_user.id, "workspace.view") is None
        # The view layer still sees the row to answer the honest 403.
        seen = get_membership(ws.id, other_user.id, include_suspended=True)
        assert seen is not None and seen.suspension_reason == SUSPENSION_NO_MFA

    def test_check_membership_and_check_capability_filter(self, user, other_user):
        ws = _ws(user)
        member = _member(ws, other_user)
        services.suspend_member(member, reason=SUSPENSION_NO_MFA)

        result = call(
            "workspaces.check_membership",
            {"workspace_id": str(ws.id), "user_id": str(other_user.pk)},
        )
        assert result == {"is_member": False, "role": None, "capabilities": []}

        result = call(
            "workspaces.check_capability",
            {
                "workspace_id": str(ws.id),
                "user_id": str(other_user.pk),
                "capability": "workspace.view",
            },
        )
        assert result == {"allowed": False, "role": None}

    def test_internal_http_membership_404_for_suspended(
        self, api_client, user, other_user
    ):
        from django.test import override_settings

        ws = _ws(user)
        member = _member(ws, other_user)
        url = f"{BASE}/internal/{ws.id}/members/{other_user.pk}"
        with override_settings(
            MIDDLEWARE=[
                "stapel_core.django.jwt.middleware.ServiceAPIKeyMiddleware"
            ],
            SERVICE_API_KEY="svc-key",
        ):
            resp = api_client.get(url, HTTP_X_API_KEY="svc-key")
            assert resp.status_code == 200, resp.content

            services.suspend_member(member, reason=SUSPENSION_NO_MFA)
            resp = api_client.get(url, HTTP_X_API_KEY="svc-key")
            assert resp.status_code == 404

    def test_api_answers_membership_suspended_with_reason(
        self, api_client, user, other_user
    ):
        ws = _ws(user)
        member = _member(ws, other_user)
        services.suspend_member(member, reason=SUSPENSION_NO_MFA)
        api_client.force_authenticate(user=other_user)
        resp = api_client.get(f"{BASE}/{ws.id}")
        assert resp.status_code == 403
        body = resp.json()
        assert body["localizable_error"] == ERR_403_MEMBERSHIP_SUSPENDED
        assert body["params"] == {"reason": SUSPENSION_NO_MFA}

    def test_workspace_list_hides_suspended_membership(
        self, api_client, user, other_user
    ):
        ws = _ws(user)
        member = _member(ws, other_user)
        api_client.force_authenticate(user=other_user)
        assert len(api_client.get(f"{BASE}/").json()["workspaces"]) == 1

        services.suspend_member(member, reason=SUSPENSION_NO_MFA)
        assert api_client.get(f"{BASE}/").json()["workspaces"] == []

    def test_members_list_shows_suspension_and_provisioned_status(
        self, authed_client, user, other_user
    ):
        ws = _ws(user)
        member = _member(ws, other_user, provisioned=True)
        services.suspend_member(member, reason=SUSPENSION_NO_MFA)
        resp = authed_client.get(f"{BASE}/{ws.id}/members")
        assert resp.status_code == 200, resp.content
        rows = {row["user_id"]: row for row in resp.json()["items"]}
        suspended_row = rows[str(other_user.pk)]
        assert suspended_row["provisioned"] is True
        assert suspended_row["suspended_at"] is not None
        assert suspended_row["suspension_reason"] == SUSPENSION_NO_MFA
        owner_row = rows[str(user.pk)]
        assert owner_row["provisioned"] is False
        assert owner_row["suspended_at"] is None
        assert owner_row["suspension_reason"] is None


@pytest.mark.django_db
class TestSuspendUnsuspendMechanics:
    def test_suspend_emits_and_invalidates_cache(
        self, user, other_user, capture, sent_notifications
    ):
        ws = _ws(user)
        member = _member(ws, other_user)
        key = _cache_key(ws.id, other_user.pk)
        cache.set(key, "member", 30)
        events = capture("workspace.member_suspended")

        assert services.suspend_member(member, reason=SUSPENSION_NO_MFA) is True
        assert cache.get(key) is None
        (event,) = events
        assert event.payload == {
            "workspace_id": str(ws.id),
            "user_id": str(other_user.pk),
            "role": "member",
            "reason": SUSPENSION_NO_MFA,
        }
        _validate(event.payload, "workspace.member_suspended")
        (notification_type, kwargs), = sent_notifications
        assert notification_type == "workspace.mfa_suspension"
        assert kwargs["user_id"] == str(other_user.pk)
        assert kwargs["variables"]["workspace_name"] == ws.name
        assert kwargs["variables"]["security_url"]

    def test_suspend_idempotent(self, user, other_user, capture):
        ws = _ws(user)
        member = _member(ws, other_user)
        events = capture("workspace.member_suspended")
        assert services.suspend_member(member, reason=SUSPENSION_NO_MFA) is True
        assert services.suspend_member(member, reason=SUSPENSION_NO_MFA) is False
        assert len(events) == 1

    def test_unsuspend_emits_notifies_and_invalidates_cache(
        self, user, other_user, capture, sent_notifications
    ):
        ws = _ws(user)
        member = _member(ws, other_user)
        services.suspend_member(member, reason=SUSPENSION_NO_MFA)
        sent_notifications.clear()
        key = _cache_key(ws.id, other_user.pk)
        cache.set(key, "__none__", 30)
        events = capture("workspace.member_unsuspended")

        assert services.unsuspend_member(member) is True
        assert cache.get(key) is None
        member.refresh_from_db()
        assert member.suspended_at is None
        assert member.suspension_reason == ""
        assert has_capability(ws.id, other_user.id, "workspace.view")
        (event,) = events
        assert event.payload == {
            "workspace_id": str(ws.id),
            "user_id": str(other_user.pk),
            "role": "member",
            "reason": SUSPENSION_NO_MFA,
        }
        _validate(event.payload, "workspace.member_unsuspended")
        (notification_type, kwargs), = sent_notifications
        assert notification_type == "workspace.mfa_restored"
        assert kwargs["variables"]["workspace_url"].endswith(ws.slug)
        # Idempotent: lifting an active membership is a no-op.
        assert services.unsuspend_member(member) is False
        assert len(events) == 1


@pytest.mark.django_db
class TestRequireMfaPatch:
    """PATCH workspace with the security block: HIGH gate + sync sweep."""

    def _patch_security(self, client, ws, security):
        return client.patch(
            f"{BASE}/{ws.id}", {"settings": {"security": security}}, format="json"
        )

    def test_requires_step_up_grant(self, authed_client, user):
        ws = _ws(user)
        resp = self._patch_security(authed_client, ws, {"require_mfa": True})
        assert resp.status_code == 403
        body = resp.json()
        assert body["verification"]["scope"] == "sensitive"
        ws.refresh_from_db()
        assert ws.settings == {}  # nothing saved without the grant

    def test_requires_security_manage_capability(
        self, api_client, user, other_user, settings
    ):
        # A custom role holding workspace.update but NOT
        # workspace.security.manage: ordinary PATCH works, security PATCH 403s.
        settings.STAPEL_WORKSPACES = {
            "ROLES": {
                "ops": {
                    "rank": 250,
                    "capabilities": ["workspace.view", "workspace.update"],
                },
            },
        }
        ws = _ws(user)
        _member(ws, other_user, role="ops")
        grant_verification(
            user_id=str(other_user.pk), scope="sensitive", max_age=300
        )
        api_client.force_authenticate(user=other_user)
        resp = self._patch_security(api_client, ws, {"require_mfa": True})
        assert resp.status_code == 403
        body = resp.json()
        assert body["localizable_error"] == ERR_403_MISSING_CAPABILITY
        assert body["params"] == {"capability": "workspace.security.manage"}
        resp = api_client.patch(
            f"{BASE}/{ws.id}", {"name": "Renamed"}, format="json"
        )
        assert resp.status_code == 200, resp.content

    def test_ordinary_patch_needs_no_step_up(self, authed_client, user):
        ws = _ws(user)
        resp = authed_client.patch(
            f"{BASE}/{ws.id}", {"name": "Renamed"}, format="json"
        )
        assert resp.status_code == 200, resp.content

    def test_invalid_security_block_400(self, authed_client, user):
        ws = _ws(user)
        grant_verification(user_id=str(user.pk), scope="sensitive", max_age=300)
        resp = self._patch_security(authed_client, ws, {"require_mfa": "yes"})
        assert resp.status_code == 400
        resp = self._patch_security(
            authed_client, ws, {"provisioned_user_policy": "none"}
        )
        assert resp.status_code == 400

    def test_enabling_sweeps_members_without_strong_mfa(
        self, authed_client, user, other_user, fake_mfa_status, capture,
        sent_notifications,
    ):
        ws = _ws(user)
        member = _member(ws, other_user)
        fake_mfa_status["strong"] = {str(user.pk)}  # owner has TOTP
        grant_verification(user_id=str(user.pk), scope="sensitive", max_age=300)
        events = capture("workspace.member_suspended")

        resp = self._patch_security(authed_client, ws, {"require_mfa": True})
        assert resp.status_code == 200, resp.content
        ws.refresh_from_db()
        assert ws.settings["security"]["require_mfa"] is True
        # Every active member was asked.
        asked = {c["user_id"] for c in fake_mfa_status["calls"]}
        assert asked == {str(user.pk), str(other_user.pk)}
        # No strong factor -> suspended (reason no_mfa) + emit + letter.
        member.refresh_from_db()
        assert member.suspension_reason == SUSPENSION_NO_MFA
        assert member.suspended_at is not None
        assert len(events) == 1
        # The owner (strong factor) is untouched.
        assert get_membership(ws.id, user.id) is not None
        assert [t for t, _ in sent_notifications] == ["workspace.mfa_suspension"]

    def test_auth_down_saves_policy_but_touches_nobody(
        self, authed_client, user, other_user, capture
    ):
        """Fail-open by suspension: auth.mfa_status not wired -> the sweep
        aborts, no member is suspended; the policy itself still saves and
        the mfa-event consumer catches up later."""
        ws = _ws(user)
        member = _member(ws, other_user)
        grant_verification(user_id=str(user.pk), scope="sensitive", max_age=300)
        events = capture("workspace.member_suspended")

        resp = self._patch_security(authed_client, ws, {"require_mfa": True})
        assert resp.status_code == 200, resp.content
        ws.refresh_from_db()
        assert ws.settings["security"]["require_mfa"] is True
        member.refresh_from_db()
        assert member.suspended_at is None
        assert events == []

    def test_disabling_lifts_no_mfa_suspensions_quietly(
        self, authed_client, user, other_user, fake_mfa_status, capture,
        sent_notifications,
    ):
        ws = _ws(user)
        ws.settings = {"security": {"require_mfa": True}}
        ws.save(update_fields=["settings"])
        member = _member(ws, other_user)
        services.suspend_member(
            member, reason=SUSPENSION_NO_MFA, notify=False
        )
        grant_verification(user_id=str(user.pk), scope="sensitive", max_age=300)
        events = capture("workspace.member_unsuspended")
        sent_notifications.clear()

        resp = self._patch_security(authed_client, ws, {"require_mfa": False})
        assert resp.status_code == 200, resp.content
        member.refresh_from_db()
        assert member.suspended_at is None
        assert len(events) == 1
        # No mfa_restored letter — its wording is about the USER enabling
        # 2FA, wrong for the org dropping the policy.
        assert sent_notifications == []


@pytest.mark.django_db
class TestMfaEventConsumers:
    def test_mfa_disabled_suspends_only_require_mfa_workspaces(
        self, user, other_user, capture, sent_notifications
    ):
        strict = _ws(user, name="Strict")
        strict.settings = {"security": {"require_mfa": True}}
        strict.save(update_fields=["settings"])
        lax = _ws(user, name="Lax")
        strict_member = _member(strict, other_user)
        lax_member = _member(lax, other_user)
        events = capture("workspace.member_suspended")

        handle_user_mfa_disabled(
            types.SimpleNamespace(
                payload={"user_id": str(other_user.pk), "factor": "totp"},
                event_id="e1",
            )
        )
        strict_member.refresh_from_db()
        lax_member.refresh_from_db()
        assert strict_member.suspension_reason == SUSPENSION_NO_MFA
        assert lax_member.suspended_at is None
        assert len(events) == 1
        assert [t for t, _ in sent_notifications] == ["workspace.mfa_suspension"]

        # Idempotent: at-least-once redelivery is a no-op.
        handle_user_mfa_disabled(
            types.SimpleNamespace(
                payload={"user_id": str(other_user.pk), "factor": "totp"},
                event_id="e1",
            )
        )
        assert len(events) == 1
        assert len(sent_notifications) == 1

    def test_mfa_enabled_lifts_only_no_mfa_suspensions(
        self, user, other_user, capture, sent_notifications
    ):
        ws1 = _ws(user, name="One")
        ws2 = _ws(user, name="Two")
        no_mfa_member = _member(ws1, other_user)
        other_reason_member = _member(ws2, other_user)
        services.suspend_member(
            no_mfa_member, reason=SUSPENSION_NO_MFA, notify=False
        )
        services.suspend_member(
            other_reason_member, reason="abuse", notify=False
        )
        events = capture("workspace.member_unsuspended")
        sent_notifications.clear()

        handle_user_mfa_enabled(
            types.SimpleNamespace(
                payload={"user_id": str(other_user.pk), "factor": "totp"},
                event_id="e2",
            )
        )
        no_mfa_member.refresh_from_db()
        other_reason_member.refresh_from_db()
        assert no_mfa_member.suspended_at is None
        # Only the no_mfa reason is MFA's to lift.
        assert other_reason_member.suspension_reason == "abuse"
        assert len(events) == 1
        assert [t for t, _ in sent_notifications] == ["workspace.mfa_restored"]

        # Idempotent redelivery.
        handle_user_mfa_enabled(
            types.SimpleNamespace(
                payload={"user_id": str(other_user.pk), "factor": "totp"},
                event_id="e2",
            )
        )
        assert len(events) == 1

    def test_missing_user_id_is_logged_not_raised(self):
        handle_user_mfa_disabled(
            types.SimpleNamespace(payload={}, event_id="e3")
        )
        handle_user_mfa_enabled(
            types.SimpleNamespace(payload={}, event_id="e4")
        )
