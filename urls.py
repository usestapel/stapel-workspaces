"""URL configuration for the workspaces app."""

from typing import NamedTuple

from django.urls import path

from .views import (
    InternalMembershipView,
    InternalPersonalWorkspaceView,
    InvitationAcceptView,
    MemberDetailView,
    MemberInviteView,
    MemberListView,
    WorkspaceDetailView,
    WorkspaceListCreateView,
)

urlpatterns = [
    path("", WorkspaceListCreateView.as_view(), name="workspace-list"),
    path("<uuid:workspace_id>", WorkspaceDetailView.as_view(), name="workspace-detail"),
    path(
        "<uuid:workspace_id>/members",
        MemberListView.as_view(),
        name="workspace-members",
    ),
    path(
        "<uuid:workspace_id>/members/invite",
        MemberInviteView.as_view(),
        name="workspace-member-invite",
    ),
    path(
        "<uuid:workspace_id>/members/<uuid:user_id>",
        MemberDetailView.as_view(),
        name="workspace-member-detail",
    ),
    path(
        "invitations/accept",
        InvitationAcceptView.as_view(),
        name="workspace-invitation-accept",
    ),
    # Internal API for service-to-service membership checks
    path(
        "internal/<uuid:workspace_id>/members/<uuid:user_id>",
        InternalMembershipView.as_view(),
        name="workspace-internal-membership",
    ),
    # Internal API: get-or-create personal workspace for a user
    path(
        "internal/users/<uuid:user_id>/personal",
        InternalPersonalWorkspaceView.as_view(),
        name="workspace-internal-personal",
    ),
]


class GateEntry(NamedTuple):
    """One gated URL block: which flags gate which url patterns (capability-config.md §2 p.2).

    ``flags`` compose with OR — the block is mounted while ANY flag is on,
    and disappears only when ALL of them are off. Empty flags = always on.
    """
    name: str
    flags: tuple
    patterns: tuple


#: Gate registry (capability-config.md §2 p.2): workspaces has no settings
#: namespace at all (no conf.py) and therefore no config gates — the whole
#: URL surface is a single always-on block. Declared as a registry entry
#: (rather than left implicit) so the capabilities.json emitter has a
#: uniform mechanism across every module.
GATE_REGISTRY: dict = {
    'workspaces.api': GateEntry('workspaces.api', (), tuple(urlpatterns)),
}
