"""PATCH workspace merges settings, and any change to security is guarded.

PATCH used to replace ``settings`` with the request body, and the
``workspace.security.manage`` + step-up branch fired only when the payload
carried a ``security`` key. So ``{"settings": {}}`` from a role holding just
``workspace.update`` erased ``require_mfa``, with no step-up and no sweep.
"""
import pytest
from django.test import override_settings
from django.utils import timezone

from stapel_core.comm.registry import function_registry
from stapel_core.verification import grant_verification

from stapel_workspaces import services
from stapel_workspaces.errors import ERR_403_MISSING_CAPABILITY
from stapel_workspaces.models import Workspace, WorkspaceMember

BASE = "/workspaces/api/workspaces/v1"
EDITOR = {"ROLES": {"editor": {"rank": 250, "capabilities": [
    "workspace.view", "workspace.update",
]}}}


@pytest.fixture
def mfa_ok(user, other_user):
    """Both members hold a strong factor, so require_mfa admits them."""
    strong = {str(user.pk), str(other_user.pk)}
    function_registry.register(
        services.MFA_STATUS, lambda p: {"has_strong_mfa": p["user_id"] in strong}
    )
    yield
    function_registry._providers.pop(services.MFA_STATUS, None)
    function_registry._schemas.pop(services.MFA_STATUS, None)


@pytest.fixture
def org(user, other_user, mfa_ok):
    ws = services.create_workspace(user=user, name="Acme")
    ws.settings = {"theme": "light", "security": {
        "require_mfa": True, "provisioned_user_policies": ["password_change"],
    }}
    ws.save()
    WorkspaceMember.objects.create(
        workspace=ws, user=other_user, role="editor", accepted_at=timezone.now()
    )
    services.enforce_require_mfa(ws)
    return ws


def _patch(client, ws, settings):
    return client.patch(f"{BASE}/{ws.id}", {"settings": settings}, format="json")


def _security(ws):
    return Workspace.objects.get(pk=ws.pk).settings.get("security")


@pytest.mark.django_db
class TestEditorCannotTouchSecurity:
    @pytest.fixture(autouse=True)
    def _editor_role(self):
        with override_settings(STAPEL_WORKSPACES=EDITOR):
            yield

    @pytest.fixture
    def editor(self, api_client, other_user):
        api_client.force_authenticate(user=other_user)
        return api_client

    def test_empty_settings_keep_require_mfa(self, editor, org):
        resp = _patch(editor, org, {})
        assert resp.status_code == 200, resp.content
        assert _security(org)["require_mfa"] is True

    def test_other_key_merges_and_keeps_security(self, editor, org):
        resp = _patch(editor, org, {"theme": "dark"})
        assert resp.status_code == 200, resp.content
        stored = Workspace.objects.get(pk=org.pk).settings
        assert stored["theme"] == "dark"
        assert stored["security"]["require_mfa"] is True

    def test_removing_security_is_refused(self, editor, org):
        resp = _patch(editor, org, {"security": None})
        assert resp.status_code == 403
        assert resp.json()["localizable_error"] == ERR_403_MISSING_CAPABILITY
        assert _security(org)["require_mfa"] is True

    def test_changing_security_is_refused(self, editor, org):
        resp = _patch(editor, org, {"security": {"require_mfa": False}})
        assert resp.status_code == 403
        assert _security(org)["require_mfa"] is True

    def test_resending_the_same_security_is_not_a_change(self, editor, org):
        resp = _patch(editor, org, {"theme": "dark", "security": {"require_mfa": True}})
        assert resp.status_code == 200, resp.content


@pytest.mark.django_db
class TestOwnerStillNeedsStepUp:
    def test_removing_security_demands_step_up(self, authed_client, org):
        resp = _patch(authed_client, org, {"security": None})
        assert resp.status_code == 403
        assert resp.json()["verification"]["scope"] == "sensitive"
        assert _security(org)["require_mfa"] is True

    def test_with_step_up_a_partial_security_block_merges(self, authed_client, user, org):
        grant_verification(user_id=str(user.pk), scope="sensitive", max_age=300)
        resp = _patch(authed_client, org, {"security": {"require_mfa": False}})
        assert resp.status_code == 200, resp.content
        assert _security(org) == {
            "require_mfa": False, "provisioned_user_policies": ["password_change"],
        }


def test_merge_rule():
    merged = services.merge_settings_patch(
        {"a": 1, "b": 2, "security": {"x": 1, "y": 2}},
        {"b": None, "c": 3, "security": {"y": None, "z": 3}},
    )
    assert merged == {"a": 1, "c": 3, "security": {"x": 1, "z": 3}}
