"""URL configuration for the workspaces app."""

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
