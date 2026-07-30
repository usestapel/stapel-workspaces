"""URL configuration for the workspaces app."""

from typing import NamedTuple

from django.urls import path

from .errors import WorkspacesErrorKeysView
from .views import (
    InternalMembershipView,
    InternalPersonalWorkspaceView,
    InvitationAcceptView,
    InvitationClaimView,
    InvitationDeclineView,
    InvitationPreviewView,
    InvitationResendView,
    InvitationRevokeView,
    MemberDetailView,
    MemberInviteView,
    MemberListView,
    MemberProvisionView,
    RoleListView,
    WorkspaceDetailView,
    WorkspaceInvitationListView,
    WorkspaceListCreateView,
)

urlpatterns = [
    path("", WorkspaceListCreateView.as_view(), name="workspace-list"),
    path("roles", RoleListView.as_view(), name="workspace-roles"),
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
        "<uuid:workspace_id>/members/provision",
        MemberProvisionView.as_view(),
        name="workspace-member-provision",
    ),
    path(
        "<uuid:workspace_id>/members/<uuid:user_id>",
        MemberDetailView.as_view(),
        name="workspace-member-detail",
    ),
    # Admin-side invitation surface (#109): who has not accepted, and the
    # two actions on such a row. Workspace-scoped and capability-gated —
    # distinct from the public, token-addressed `invitations/<token>`
    # routes below, which the invitee uses without a session.
    path(
        "<uuid:workspace_id>/invitations",
        WorkspaceInvitationListView.as_view(),
        name="workspace-invitation-list",
    ),
    path(
        "<uuid:workspace_id>/invitations/<uuid:invitation_id>/revoke",
        InvitationRevokeView.as_view(),
        name="workspace-invitation-revoke",
    ),
    path(
        "<uuid:workspace_id>/invitations/<uuid:invitation_id>/resend",
        InvitationResendView.as_view(),
        name="workspace-invitation-resend",
    ),
    path(
        "invitations/accept",
        InvitationAcceptView.as_view(),
        name="workspace-invitation-accept",
    ),
    # Public invite-flow surface (org-program spec §B2). Declared AFTER the
    # literal invitations/accept route so "accept" can never be read as a
    # token (real tokens are 43-char urlsafe strings anyway).
    path(
        "invitations/<str:token>",
        InvitationPreviewView.as_view(),
        name="workspace-invitation-preview",
    ),
    path(
        "invitations/<str:token>/decline",
        InvitationDeclineView.as_view(),
        name="workspace-invitation-decline",
    ),
    path(
        "invitations/<str:token>/claim",
        InvitationClaimView.as_view(),
        name="workspace-invitation-claim",
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
    # Error-key registry for the stapel-translate collector (service/staff only).
    path("error-keys/", WorkspacesErrorKeysView.as_view(), name="error-keys"),
]


class GateEntry(NamedTuple):
    """One gated URL block: which flags gate which url patterns (capability-config.md §2 p.2).

    ``flags`` compose with OR — the block is mounted while ANY flag is on,
    and disappears only when ALL of them are off. Empty flags = always on.
    """
    name: str
    flags: tuple
    patterns: tuple


#: Gate registry (capability-config.md §2 p.2): workspaces has a settings
#: namespace (conf.py — role/capability registries and tuning knobs) but no
#: boolean feature gates — the whole URL surface is a single always-on
#: block. Declared as a registry entry (rather than left implicit) so the
#: capabilities.json emitter has a uniform mechanism across every module.
GATE_REGISTRY: dict = {
    'workspaces.api': GateEntry('workspaces.api', (), tuple(urlpatterns)),
}
