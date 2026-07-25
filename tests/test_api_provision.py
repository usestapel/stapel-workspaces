"""POST members/provision — org-created (synthetic) users (spec §C1, Wave 3).

The auth side (``auth.provision_user``) is faked via the comm function
registry — only its payload/result contract is shared. The HIGH step-up
gate uses the real ``stapel_core.verification`` grant store (scope
``sensitive``), seeded per test via ``grant_verification``.
"""

import json
from pathlib import Path

import jsonschema
import pytest
from django.utils import timezone

from stapel_core.comm import subscribe_action
from stapel_core.comm.registry import function_registry
from stapel_core.verification import grant_verification

import stapel_workspaces
from stapel_workspaces import entitlements, services
from stapel_workspaces.errors import (
    ERR_400_INVALID_PROVISION_USERNAME,
    ERR_400_INVALID_ROLE,
    ERR_402_ENTITLEMENT_REQUIRED,
    ERR_403_MISSING_CAPABILITY,
    ERR_503_AUTH_UNAVAILABLE,
)
from stapel_workspaces.models import Role, WorkspaceMember

BASE = "/workspaces/api/workspaces/v1"
SCHEMAS_DIR = Path(stapel_workspaces.__file__).resolve().parent / "schemas" / "emits"

#: The structured-failure keys auth may answer with (passed through keyed).
ERR_409_USERNAME_TAKEN = "error.409.username_taken"


def _ws(user, name="Acme"):
    return services.create_workspace(user=user, name=name)


def _register(name, provider):
    function_registry.register(name, provider)


def _unregister(name):
    function_registry._providers.pop(name, None)
    function_registry._schemas.pop(name, None)


@pytest.fixture
def sensitive_grant(user):
    """Seed a fresh HIGH step-up grant (scope ``sensitive``) for *user*."""
    grant_verification(user_id=str(user.pk), scope="sensitive", max_age=300)


@pytest.fixture
def fake_auth_provision(db):
    """Scripted ``auth.provision_user`` provider.

    Default behavior mirrors the real contract: creates a real user row
    per call (the workspaces side needs one for the member FK — in a
    monolith the comm call lands in the same DB) and returns its pk plus
    ``generated_password`` when the payload carried no password. Set
    ``response`` to a dict/callable to script failures.
    """
    import uuid as _uuid

    from stapel_core.django.users.models import User

    state = {"calls": [], "response": None, "created": []}

    def provider(payload):
        state["calls"].append(payload)
        if state["response"] is not None:
            response = state["response"]
            return response(payload) if callable(response) else response
        created = User.objects.create_user(
            username=payload["username"],
            email=payload.get("email") or f"{_uuid.uuid4().hex[:8]}@example.com",
            password=payload.get("password") or "srv-generated-1",
        )
        state["created"].append(created)
        result = {"user_id": str(created.pk)}
        if not payload.get("password"):
            result["generated_password"] = "srv-generated-1"
        return result

    _register(services.PROVISION_USER, provider)
    yield state
    _unregister(services.PROVISION_USER)


@pytest.fixture
def capture():
    def _capture(name):
        events = []
        subscribe_action(name, events.append)
        return events

    return _capture


def _provision(client, ws, body=None):
    payload = {"username_local": "jdoe", "role": "member"}
    payload.update(body or {})
    return client.post(
        f"{BASE}/{ws.id}/members/provision", payload, format="json"
    )


@pytest.mark.django_db
class TestProvisionHappyPath:
    def test_generated_password_returned_once_member_created(
        self, authed_client, user, sensitive_grant, fake_auth_provision, capture,
    ):
        ws = _ws(user)
        events = capture("workspace.member_provisioned")
        resp = _provision(authed_client, ws)
        assert resp.status_code == 201, resp.content
        (created,) = fake_auth_provision["created"]
        body = resp.json()
        assert body["user_id"] == str(created.pk)
        assert body["username"] == f"{ws.slug}/jdoe"
        assert body["role"] == "member"
        # No email anchor -> the server-generated password comes back in
        # the API response, exactly once (nothing else ever carries it).
        assert body["generated_password"] == "srv-generated-1"

        member = WorkspaceMember.objects.get(workspace=ws, user=created)
        assert member.provisioned is True
        assert member.accepted_at is not None
        assert member.suspended_at is None
        assert member.role == "member"
        assert member.invited_by == user

        # Emit: audit/metering signal, schema-validated, no credentials.
        assert len(events) == 1
        payload = events[0].payload
        assert payload == {
            "workspace_id": str(ws.id),
            "user_id": str(created.pk),
            "role": "member",
            "provisioned_by": str(user.pk),
        }
        jsonschema.validate(
            payload,
            json.loads(
                (SCHEMAS_DIR / "workspace.member_provisioned.json").read_text()
            ),
            format_checker=jsonschema.FormatChecker(),
        )
        assert "password" not in str(payload)

    def test_auth_payload_carries_full_username_and_default_policy(
        self, authed_client, user, sensitive_grant, fake_auth_provision
    ):
        ws = _ws(user)
        resp = _provision(authed_client, ws)
        assert resp.status_code == 201, resp.content
        call = fake_auth_provision["calls"][-1]
        assert call["username"] == f"{ws.slug}/jdoe"
        assert call["first_login_policy"] == "password_change"  # default
        assert "password" not in call
        assert "email" not in call

    def test_workspace_policy_mfa_enroll_forwarded(
        self, authed_client, user, sensitive_grant, fake_auth_provision
    ):
        ws = _ws(user)
        ws.settings = {"security": {"provisioned_user_policy": "mfa_enroll"}}
        ws.save(update_fields=["settings"])
        resp = _provision(authed_client, ws)
        assert resp.status_code == 201, resp.content
        call = fake_auth_provision["calls"][-1]
        assert call["first_login_policy"] == "mfa_enroll"

    def test_admin_chosen_password_not_echoed(
        self, authed_client, user, sensitive_grant, fake_auth_provision
    ):
        ws = _ws(user)
        resp = _provision(authed_client, ws, {"password": "Chosen-pass-1"})
        assert resp.status_code == 201, resp.content
        assert resp.json()["generated_password"] is None
        assert fake_auth_provision["calls"][-1]["password"] == "Chosen-pass-1"


@pytest.mark.django_db
class TestProvisionEmailNuance:
    def test_no_email_skips_letter(
        self, authed_client, user, sensitive_grant, fake_auth_provision,
        monkeypatch,
    ):
        sent = []
        monkeypatch.setattr(
            "stapel_core.notifications.request_notification",
            lambda notification_type, **kwargs: sent.append(
                (notification_type, kwargs)
            )
            or True,
        )
        ws = _ws(user)
        resp = _provision(authed_client, ws)
        assert resp.status_code == 201, resp.content
        assert sent == []  # nowhere to send it — synthetic has no email

    def test_email_sends_credentials_letter(
        self, authed_client, user, sensitive_grant, fake_auth_provision,
        monkeypatch,
    ):
        sent = []
        monkeypatch.setattr(
            "stapel_core.notifications.request_notification",
            lambda notification_type, **kwargs: sent.append(
                (notification_type, kwargs)
            )
            or True,
        )
        ws = _ws(user)
        resp = _provision(authed_client, ws, {"email": "jdoe@corp.example"})
        assert resp.status_code == 201, resp.content
        assert fake_auth_provision["calls"][-1]["email"] == "jdoe@corp.example"
        (notification_type, kwargs), = sent
        assert notification_type == "workspace.provisioned_account"
        assert kwargs["email"] == "jdoe@corp.example"
        variables = kwargs["variables"]
        assert variables["username"] == f"{ws.slug}/jdoe"
        assert variables["initial_password"] == "srv-generated-1"
        assert variables["login_url"] == "https://app.example.com/login"
        assert variables["workspace_name"] == ws.name


@pytest.mark.django_db
class TestProvisionGates:
    def test_high_grant_required(self, authed_client, user, fake_auth_provision):
        """403 verification envelope without a fresh `sensitive` grant —
        and auth is never called."""
        ws = _ws(user)
        resp = _provision(authed_client, ws)
        assert resp.status_code == 403
        body = resp.json()
        assert "verification" in body
        assert body["verification"]["scope"] == "sensitive"
        assert fake_auth_provision["calls"] == []
        assert not WorkspaceMember.objects.filter(
            workspace=ws, provisioned=True
        ).exists()

    def test_capability_required(
        self, api_client, user, other_user, fake_auth_provision
    ):
        """A plain member holds a grant but not members.provision -> 403."""
        ws = _ws(user)
        WorkspaceMember.objects.create(
            workspace=ws, user=other_user, role=Role.MEMBER,
            accepted_at=timezone.now(),
        )
        grant_verification(user_id=str(other_user.pk), scope="sensitive", max_age=300)
        api_client.force_authenticate(user=other_user)
        resp = _provision(api_client, ws)
        assert resp.status_code == 403
        assert resp.json()["localizable_error"] == ERR_403_MISSING_CAPABILITY
        assert fake_auth_provision["calls"] == []

    def test_entitlement_deny_402(
        self, authed_client, user, sensitive_grant, fake_auth_provision
    ):
        ws = _ws(user)
        state = {"calls": []}

        def deny(payload):
            state["calls"].append(payload)
            return {"allowed": False, "reason": "plan"}

        _register(entitlements.CHECK_ENTITLEMENT, deny)
        try:
            resp = _provision(authed_client, ws)
        finally:
            _unregister(entitlements.CHECK_ENTITLEMENT)
        assert resp.status_code == 402
        assert resp.json()["localizable_error"] == ERR_402_ENTITLEMENT_REQUIRED
        assert state["calls"][-1]["key"] == entitlements.ENT_PROVISION_USER
        assert state["calls"][-1]["user_id"] == str(user.pk)  # owner anchor
        assert fake_auth_provision["calls"] == []

    def test_invalid_local_username_rejected_before_auth(
        self, authed_client, user, sensitive_grant, fake_auth_provision
    ):
        ws = _ws(user)
        for bad in ("evil/name", "sp ace"):
            resp = _provision(authed_client, ws, {"username_local": bad})
            assert resp.status_code == 400, (bad, resp.content)
            assert ERR_400_INVALID_PROVISION_USERNAME in str(resp.json())
        # Empty local part: rejected by the field layer (blank), still 400.
        resp = _provision(authed_client, ws, {"username_local": ""})
        assert resp.status_code == 400
        assert fake_auth_provision["calls"] == []

    def test_owner_role_not_provisionable(
        self, authed_client, user, sensitive_grant, fake_auth_provision
    ):
        ws = _ws(user)
        resp = _provision(authed_client, ws, {"role": "owner"})
        assert resp.status_code == 400
        assert ERR_400_INVALID_ROLE in str(resp.json())
        assert fake_auth_provision["calls"] == []


@pytest.mark.django_db
class TestProvisionAuthFailures:
    def test_username_taken_passes_auth_key_through(
        self, authed_client, user, sensitive_grant, fake_auth_provision
    ):
        fake_auth_provision["response"] = {"error": ERR_409_USERNAME_TAKEN}
        ws = _ws(user)
        resp = _provision(authed_client, ws)
        assert resp.status_code == 409
        assert resp.json()["localizable_error"] == ERR_409_USERNAME_TAKEN
        assert not WorkspaceMember.objects.filter(
            workspace=ws, provisioned=True
        ).exists()

    def test_auth_unavailable_503(self, authed_client, user, sensitive_grant):
        ws = _ws(user)
        resp = _provision(authed_client, ws)  # no provider registered
        assert resp.status_code == 503
        assert resp.json()["localizable_error"] == ERR_503_AUTH_UNAVAILABLE


@pytest.mark.django_db
class TestProvisionDebit:
    @pytest.fixture
    def fake_debit(self):
        state = {"calls": [], "response": {"ok": True, "balance": 95}}

        def provider(payload):
            state["calls"].append(payload)
            response = state["response"]
            return response(payload) if callable(response) else response

        _register(entitlements.DEBIT, provider)
        yield state
        _unregister(entitlements.DEBIT)

    def test_no_debit_when_free(
        self, authed_client, user, sensitive_grant, fake_auth_provision,
        fake_debit,
    ):
        ws = _ws(user)
        resp = _provision(authed_client, ws)
        assert resp.status_code == 201, resp.content
        assert fake_debit["calls"] == []  # PROVISION_USER_CREDITS default 0

    def test_debit_charged_with_idempotency_key(
        self, authed_client, user, sensitive_grant, fake_auth_provision,
        fake_debit, settings,
    ):
        settings.STAPEL_WORKSPACES = {"PROVISION_USER_CREDITS": 5}
        ws = _ws(user)
        resp = _provision(authed_client, ws)
        assert resp.status_code == 201, resp.content
        (call,) = fake_debit["calls"]
        assert call["user_id"] == str(user.pk)  # billing anchor = owner
        assert call["credits"] == 5
        assert call["idempotency_key"].startswith("ws-provision:")
        assert call["metadata"]["workspace_id"] == str(ws.id)
        assert call["metadata"]["username"] == f"{ws.slug}/jdoe"

        # A second provisioning is a NEW paid action: fresh provision uuid,
        # fresh idempotency key (the key only dedupes comm-level retries of
        # the same attempt).
        resp = _provision(authed_client, ws, {"username_local": "asmith"})
        assert resp.status_code == 201, resp.content
        keys = [c["idempotency_key"] for c in fake_debit["calls"]]
        assert len(set(keys)) == 2

    def test_debit_refused_402_and_no_user_created(
        self, authed_client, user, sensitive_grant, fake_auth_provision,
        fake_debit, settings,
    ):
        settings.STAPEL_WORKSPACES = {"PROVISION_USER_CREDITS": 5}
        fake_debit["response"] = {"ok": False, "reason": "insufficient_credits"}
        ws = _ws(user)
        resp = _provision(authed_client, ws)
        assert resp.status_code == 402
        assert resp.json()["localizable_error"] == ERR_402_ENTITLEMENT_REQUIRED
        assert fake_auth_provision["calls"] == []  # refused before auth
        assert not WorkspaceMember.objects.filter(
            workspace=ws, provisioned=True
        ).exists()

    def test_billing_absent_degrades_to_allow(
        self, authed_client, user, sensitive_grant, fake_auth_provision,
        settings,
    ):
        settings.STAPEL_WORKSPACES = {"PROVISION_USER_CREDITS": 5}
        ws = _ws(user)
        resp = _provision(authed_client, ws)  # no billing.debit registered
        assert resp.status_code == 201, resp.content
