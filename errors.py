"""Custom error keys for the workspaces service."""

from stapel_core.django.api.errors import ErrorKeysView, register_service_errors

ERR_404_WORKSPACE_NOT_FOUND = "error.404.workspace_not_found"
ERR_404_MEMBER_NOT_FOUND = "error.404.member_not_found"
ERR_404_INVITATION_NOT_FOUND = "error.404.invitation_not_found"
ERR_403_FORBIDDEN_WORKSPACE = "error.403.forbidden_workspace"
ERR_403_LAST_OWNER = "error.403.last_owner_cannot_be_removed"
ERR_400_SLUG_TAKEN = "error.400.workspace_slug_taken"
ERR_400_ALREADY_MEMBER = "error.400.already_workspace_member"
ERR_400_INVITATION_EXPIRED = "error.400.invitation_expired"
ERR_400_INVITATION_ALREADY_USED = "error.400.invitation_already_used"
ERR_400_INVITATION_REVOKED = "error.400.invitation_revoked"
ERR_400_INVALID_ROLE = "error.400.invalid_role"

WORKSPACES_ERRORS = {
    ERR_404_WORKSPACE_NOT_FOUND: "Workspace not found",
    ERR_404_MEMBER_NOT_FOUND: "Member not found in this workspace",
    ERR_404_INVITATION_NOT_FOUND: "Invitation not found",
    ERR_403_FORBIDDEN_WORKSPACE: "You do not have access to this workspace",
    ERR_403_LAST_OWNER: "The last owner cannot be removed; transfer ownership first",
    ERR_400_SLUG_TAKEN: "Workspace slug is already taken",
    ERR_400_ALREADY_MEMBER: "User is already a member of this workspace",
    ERR_400_INVITATION_EXPIRED: "Invitation has expired",
    ERR_400_INVITATION_ALREADY_USED: "Invitation has already been used",
    ERR_400_INVITATION_REVOKED: "Invitation has been revoked",
    ERR_400_INVALID_ROLE: "Invalid role",
}

register_service_errors(WORKSPACES_ERRORS)


class WorkspacesErrorKeysView(ErrorKeysView):
    def get_service_errors(self):
        return WORKSPACES_ERRORS
