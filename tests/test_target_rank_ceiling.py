"""A member-management capability does not reach members who outrank you.

``_rank_check`` capped the role handed out, but change/remove/reset/rename
protected only OWNER targets. A product role below admin carrying
``members.role.change``/``members.remove``/``members.password.reset`` could
demote, remove or reset an admin.
"""
import uuid

import pytest
from django.test import override_settings
from django.utils import timezone

from stapel_core.django.users.models import User
from stapel_core.verification import grant_verification

from stapel_workspaces import services
from stapel_workspaces.errors import ERR_403_ROLE_EXCEEDS_INVITER_RANK
from stapel_workspaces.models import Role, WorkspaceMember

BASE = "/workspaces/api/workspaces/v1"
MANAGER = {"ROLES": {"manager": {"rank": 250, "capabilities": [
    "workspace.view", "members.view", "members.role.change",
    "members.remove", "members.password.reset",
]}}}


def _user():
    return User.objects.create_user(
        username=f"u-{uuid.uuid4().hex[:8]}", email=f"{uuid.uuid4().hex[:8]}@example.com",
    )


@pytest.fixture
def org(user):
    with override_settings(STAPEL_WORKSPACES=MANAGER):
        ws = services.create_workspace(user=user, name="Acme")
        manager, admin, member = _user(), _user(), _user()
        for u, role in ((manager, "manager"), (admin, Role.ADMIN), (member, Role.MEMBER)):
            WorkspaceMember.objects.create(
                workspace=ws, user=u, role=role, accepted_at=timezone.now()
            )
        yield ws, manager, admin, member


def _refused(resp):
    assert resp.status_code == 403, resp.content
    assert resp.json()["localizable_error"] == ERR_403_ROLE_EXCEEDS_INVITER_RANK


@pytest.mark.django_db
class TestManagerCannotActOnAnAdmin:
    def test_demote(self, api_client, org):
        ws, manager, admin, _ = org
        api_client.force_authenticate(user=manager)
        _refused(api_client.patch(
            f"{BASE}/{ws.id}/members/{admin.id}", {"role": "member"}, format="json"
        ))
        assert WorkspaceMember.objects.get(workspace=ws, user=admin).role == Role.ADMIN

    def test_remove(self, api_client, org):
        ws, manager, admin, _ = org
        api_client.force_authenticate(user=manager)
        _refused(api_client.delete(f"{BASE}/{ws.id}/members/{admin.id}"))
        assert WorkspaceMember.objects.filter(workspace=ws, user=admin).exists()

    def test_password_reset(self, api_client, org):
        ws, manager, admin, _ = org
        api_client.force_authenticate(user=manager)
        WorkspaceMember.objects.filter(workspace=ws, user=admin).update(provisioned=True)
        grant_verification(user_id=str(manager.pk), scope="sensitive", max_age=300)
        _refused(api_client.post(
            f"{BASE}/{ws.id}/members/{admin.id}/password/reset", {}, format="json"
        ))

    def test_a_lower_rank_target_is_still_reachable(self, api_client, org):
        ws, manager, _, member = org
        api_client.force_authenticate(user=manager)
        resp = api_client.patch(
            f"{BASE}/{ws.id}/members/{member.id}", {"role": "viewer"}, format="json"
        )
        assert resp.status_code == 200, resp.content
