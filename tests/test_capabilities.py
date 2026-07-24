"""Role/capability registry tests (org-program spec §A1).

Covers the settings-registry (builtin + ``STAPEL_WORKSPACES["ROLES"]``
last-wins overlay), the owner system-protection, the wildcard matcher, the
capability step-up levels, and the Django system checks guarding the
overlay's shape.
"""

from django.test import override_settings

from stapel_workspaces.capabilities import (
    BUILTIN_CAPABILITY_LEVELS,
    BUILTIN_ROLES,
    capabilities_for,
    capability_level,
    capability_matches,
    effective_roles,
    role_has_capability,
    role_rank,
)

SECRETARY = {
    "rank": 250,
    "capabilities": [
        "workspace.view", "members.view", "members.remove",
        "meetings.spotlight", "meetings.kick",
    ],
}


class TestBuiltinRegistry:
    def test_builtin_four_present_with_spec_ranks(self):
        roles = effective_roles()
        assert set(roles) == {"owner", "admin", "member", "viewer"}
        assert roles["owner"]["rank"] == 400
        assert roles["admin"]["rank"] == 300
        assert roles["member"]["rank"] == 200
        assert roles["viewer"]["rank"] == 100

    def test_owner_grants_everything(self):
        assert capabilities_for("owner") == ["*"]
        assert role_has_capability("owner", "anything.at_all")

    def test_admin_grants_spec_capability_set(self):
        assert capabilities_for("admin") == [
            "workspace.view", "workspace.update",
            "members.view", "members.invite", "members.remove",
            "members.role.change", "members.provision",
            "workspace.security.manage",
        ]

    def test_member_and_viewer_are_view_only(self):
        for role in ("member", "viewer"):
            assert capabilities_for(role) == ["workspace.view", "members.view"]
            assert not role_has_capability(role, "members.invite")

    def test_unknown_role_denies_by_default(self):
        assert capabilities_for("ghost") == []
        assert role_rank("ghost") is None
        assert not role_has_capability("ghost", "workspace.view")


class TestOverlay:
    @override_settings(STAPEL_WORKSPACES={"ROLES": {"secretary": SECRETARY}})
    def test_custom_role_merges_over_builtins(self):
        roles = effective_roles()
        assert set(roles) == {"owner", "admin", "member", "viewer", "secretary"}
        assert role_rank("secretary") == 250
        assert role_has_capability("secretary", "meetings.spotlight")
        assert not role_has_capability("secretary", "members.invite")
        # builtins untouched
        assert capabilities_for("admin") == BUILTIN_ROLES["admin"]["capabilities"]

    @override_settings(STAPEL_WORKSPACES={"ROLES": {
        "admin": {"rank": 300, "capabilities": ["workspace.view"]},
    }})
    def test_overlay_replaces_builtin_entry_whole_last_wins(self):
        assert capabilities_for("admin") == ["workspace.view"]
        assert not role_has_capability("admin", "members.invite")

    @override_settings(STAPEL_WORKSPACES={"ROLES": {
        "owner": {"rank": 1, "capabilities": []},
    }})
    def test_owner_cannot_be_overridden(self):
        # Runtime backstop: the builtin owner entry always wins (the system
        # check reports the misconfiguration at deploy time).
        assert role_rank("owner") == 400
        assert capabilities_for("owner") == ["*"]


class TestWildcardMatcher:
    def test_star_matches_everything(self):
        assert capability_matches("meetings.kick", "*")

    def test_exact_match(self):
        assert capability_matches("members.invite", "members.invite")
        assert not capability_matches("members.invite", "members.remove")

    def test_prefix_wildcard(self):
        assert capability_matches("members.invite", "members.*")
        assert capability_matches("members.role.change", "members.*")
        assert not capability_matches("workspace.view", "members.*")

    def test_prefix_wildcard_requires_dot_boundary(self):
        # "members.*" must not match "membership.x" — the prefix keeps the dot.
        assert not capability_matches("membership.view", "members.*")

    def test_role_with_prefix_wildcard_grant(self):
        with override_settings(STAPEL_WORKSPACES={"ROLES": {
            "moderator": {"rank": 150, "capabilities": ["members.*"]},
        }}):
            assert role_has_capability("moderator", "members.remove")
            assert not role_has_capability("moderator", "workspace.update")


class TestCapabilityLevels:
    def test_builtin_high_capabilities(self):
        assert BUILTIN_CAPABILITY_LEVELS == {
            "members.provision": "high",
            "workspace.security.manage": "high",
        }
        assert capability_level("members.provision") == "high"
        assert capability_level("members.invite") == "standard"

    @override_settings(STAPEL_WORKSPACES={"CAPABILITY_LEVELS": {
        "records.purge": "high",
        "members.provision": "standard",
    }})
    def test_overlay_merges_last_wins(self):
        assert capability_level("records.purge") == "high"
        assert capability_level("members.provision") == "standard"
        assert capability_level("workspace.security.manage") == "high"


class TestSystemChecks:
    def _role_errors(self):
        from stapel_workspaces.checks import check_roles_overlay

        return [e.id for e in check_roles_overlay(None)]

    def _level_errors(self):
        from stapel_workspaces.checks import check_capability_levels

        return [e.id for e in check_capability_levels(None)]

    def test_clean_config_no_errors(self):
        assert self._role_errors() == []
        assert self._level_errors() == []

    @override_settings(STAPEL_WORKSPACES={"ROLES": {"secretary": SECRETARY}})
    def test_valid_overlay_no_errors(self):
        assert self._role_errors() == []

    @override_settings(STAPEL_WORKSPACES={"ROLES": {
        "owner": {"rank": 500, "capabilities": ["*"]},
    }})
    def test_owner_override_flagged(self):
        assert "stapel_workspaces.E002" in self._role_errors()

    @override_settings(STAPEL_WORKSPACES={"ROLES": {
        "secretary": {"capabilities": ["workspace.view"]},
    }})
    def test_missing_rank_flagged(self):
        assert "stapel_workspaces.E004" in self._role_errors()

    @override_settings(STAPEL_WORKSPACES={"ROLES": {
        "secretary": {"rank": 250, "capabilities": "workspace.view"},
    }})
    def test_capabilities_must_be_list_of_strings(self):
        assert "stapel_workspaces.E005" in self._role_errors()

    @override_settings(STAPEL_WORKSPACES={"ROLES": {
        "secretary": {"rank": 250, "capabilities": ["workspace.view", ""]},
    }})
    def test_empty_capability_string_flagged(self):
        assert "stapel_workspaces.E005" in self._role_errors()

    @override_settings(STAPEL_WORKSPACES={"ROLES": {"secretary": "admin"}})
    def test_non_dict_entry_flagged(self):
        assert "stapel_workspaces.E003" in self._role_errors()

    @override_settings(STAPEL_WORKSPACES={"ROLES": {
        "a-role-name-way-too-long-for-the-column-width": {
            "rank": 1, "capabilities": [],
        },
    }})
    def test_role_key_over_column_width_flagged(self):
        assert "stapel_workspaces.E006" in self._role_errors()

    @override_settings(STAPEL_WORKSPACES={"CAPABILITY_LEVELS": {
        "records.purge": "extreme",
    }})
    def test_bad_level_value_flagged(self):
        assert "stapel_workspaces.E008" in self._level_errors()
