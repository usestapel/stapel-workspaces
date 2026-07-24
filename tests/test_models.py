"""
Tests for workspaces models.
"""
import pytest
from django.test import override_settings
from django.utils import timezone

from stapel_workspaces.models import Role, WorkspaceInvitation, WorkspaceMember


@pytest.mark.django_db
class TestWorkspacesModels:
    """Tests for workspaces models."""

    def test_role_columns_widened_for_registry_roles(self):
        """0.6 migration gate: role columns fit 32-char registry role keys."""
        assert WorkspaceMember._meta.get_field("role").max_length == 32
        assert WorkspaceInvitation._meta.get_field("role").max_length == 32

    def test_builtin_choices_still_declared(self):
        # Model-level choices stay on the builtin four (the stapel-recordings
        # SourceType precedent): display/admin defaults, while serializers
        # validate against the effective registry.
        field = WorkspaceMember._meta.get_field("role")
        assert [c[0] for c in field.choices] == list(Role.values)

    @override_settings(STAPEL_WORKSPACES={"ROLES": {
        "a-custom-registry-role-name-32ch": {"rank": 250, "capabilities": []},
    }})
    def test_custom_registry_role_storable(self, user, other_user):
        from stapel_workspaces.services import create_workspace

        ws = create_workspace(user=user, name="Acme")
        member = WorkspaceMember.objects.create(
            workspace=ws,
            user=other_user,
            role="a-custom-registry-role-name-32ch",
            accepted_at=timezone.now(),
        )
        member.refresh_from_db()
        assert member.role == "a-custom-registry-role-name-32ch"
        inv = WorkspaceInvitation.objects.create(
            workspace=ws,
            email="i@example.com",
            role="a-custom-registry-role-name-32ch",
            token="t" * 43,
            expires_at=timezone.now(),
        )
        inv.refresh_from_db()
        assert inv.role == "a-custom-registry-role-name-32ch"
