"""Permission-layer tests: rank migration + capability checks (spec §A2).

The builtin-four compatibility test is the GATE of the 0.6 mandate model:
``role_at_least`` moved from a hardcoded list index to registry ranks and
must answer identically for every builtin pair.
"""

import pytest
from django.test import override_settings
from django.utils import timezone

from stapel_workspaces.models import Role, WorkspaceMember
from stapel_workspaces.permissions import (
    ROLE_HIERARCHY,
    get_membership,
    has_capability,
    require_capability,
    require_role,
    role_at_least,
)

SECRETARY = {
    "rank": 250,
    "capabilities": ["workspace.view", "members.view", "meetings.spotlight"],
}


class TestRoleAtLeastBackcompat:
    def test_builtin_four_pairs_match_historical_hierarchy(self):
        """GATE: rank ordering reproduces the pre-0.6 list-index semantics."""
        legacy = [Role.VIEWER, Role.MEMBER, Role.ADMIN, Role.OWNER]
        for role in legacy:
            for minimum in legacy:
                expected = legacy.index(role) >= legacy.index(minimum)
                assert role_at_least(role, minimum) is expected, (role, minimum)

    def test_role_hierarchy_export_unchanged(self):
        assert ROLE_HIERARCHY == [Role.VIEWER, Role.MEMBER, Role.ADMIN, Role.OWNER]

    def test_unknown_role_is_never_at_least(self):
        assert not role_at_least("ghost", Role.VIEWER)
        assert not role_at_least(Role.OWNER, "ghost")

    @override_settings(STAPEL_WORKSPACES={"ROLES": {"secretary": SECRETARY}})
    def test_custom_role_participates_via_rank(self):
        assert role_at_least("secretary", Role.MEMBER)      # 250 >= 200
        assert not role_at_least("secretary", Role.ADMIN)   # 250 < 300
        assert role_at_least(Role.ADMIN, "secretary")       # 300 >= 250


def _membership(ws, user, role, accepted=True):
    return WorkspaceMember.objects.create(
        workspace=ws,
        user=user,
        role=role,
        accepted_at=timezone.now() if accepted else None,
    )


@pytest.mark.django_db
class TestCapabilityChecks:
    @pytest.fixture
    def ws(self, user):
        from stapel_workspaces.services import create_workspace

        return create_workspace(user=user, name="Acme")

    def test_owner_has_everything(self, ws, user):
        assert has_capability(ws.id, user.id, "members.invite")
        assert has_capability(ws.id, user.id, "meetings.spotlight")

    def test_member_capability_and_denial(self, ws, other_user):
        _membership(ws, other_user, Role.MEMBER)
        assert has_capability(ws.id, other_user.id, "workspace.view")
        assert not has_capability(ws.id, other_user.id, "members.invite")

    def test_require_capability_returns_membership(self, ws, other_user):
        m = _membership(ws, other_user, Role.ADMIN)
        found = require_capability(ws.id, other_user.id, "members.invite")
        assert found is not None and found.id == m.id

    def test_pending_membership_does_not_count(self, ws, other_user):
        _membership(ws, other_user, Role.ADMIN, accepted=False)
        assert require_capability(ws.id, other_user.id, "members.view") is None
        assert not has_capability(ws.id, other_user.id, "workspace.view")

    def test_non_member_denied(self, ws, other_user):
        assert require_capability(ws.id, other_user.id, "workspace.view") is None

    @override_settings(STAPEL_WORKSPACES={"ROLES": {"secretary": SECRETARY}})
    def test_custom_role_membership_grants_custom_capability(self, ws, other_user):
        _membership(ws, other_user, "secretary")
        assert has_capability(ws.id, other_user.id, "meetings.spotlight")
        assert not has_capability(ws.id, other_user.id, "members.remove")
        # role helpers still see the membership
        assert get_membership(ws.id, other_user.id) is not None
        assert require_role(ws.id, other_user.id, Role.MEMBER) is not None
