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

# Machine-readable recovery hints (remediation) — the canonical "what to do"
# for each key, emitted into the errors.json codegen artifact and consumed by
# the frontend/LLM (frontend-core-architecture §2.5). Vocabulary: retry |
# wait_and_retry | reauthenticate | verify | fix_input | contact_support | bug.
# Declared here (backend = canon) rather than left to the status+name heuristic,
# which lies for several workspaces keys (membership/invitation cases). Rationale
# per key:
#
#   * 404 *_not_found (workspace/member/invitation) → fix_input, NOT the
#     heuristic's retry-for-404-not_found: retrying the same lookup just loops
#     the failing request. The honest recovery is to correct the identifier
#     (same canon override the notifications/profiles/billing pairs made).
#   * 403 forbidden_workspace → contact_support, NOT the heuristic's retry-for-403.
#     This is the "not a member / not authorized for this workspace" boundary.
#     The task's open question (not_a_member 403 → contact_support or fix_input?)
#     resolves to contact_support: there is no request field the user can edit to
#     grant themselves access (fix_input is wrong), and retrying loops (re-auth
#     won't help either) — the resolution is that a workspace owner/admin must
#     invite or promote them, i.e. escalate to another party (precedent: billing's
#     forbidden_billing → contact_support).
#   * 403 last_owner_cannot_be_removed → fix_input, NOT the heuristic's
#     retry-for-403. This is a self-serve precondition, not an authorization
#     wall: the message states the fix ("transfer ownership first"). Retrying the
#     same removal loops; contact_support is wrong (no operator needed). fix_input
#     is the "the request as-is can't succeed, change what you control" signal —
#     the host surfaces the transfer-ownership affordance (analogous to billing's
#     insufficient_credits → fix_input self-serve).
#   * 400 invitation_expired → contact_support, NOT the heuristic's retry. The
#     heuristic matches the `expired` token and says retry (designed for
#     restartable challenges like qr_expired), but an invitee holding an expired
#     token CANNOT self-restart — the token is dead and immutable. Retry loops on
#     it forever; fix_input is wrong (no field to edit). The only recovery is the
#     workspace owner issuing a fresh invitation — an external party — which is
#     exactly what contact_support signals. This is the sharpest lie the
#     heuristic tells for this module.
#   * 400 invitation_revoked → contact_support, NOT the heuristic's fix_input.
#     Same shape as expired: the owner deliberately killed the invite; the invitee
#     has no field to fix and must be re-invited. Escalate to the owner.
#   * 400 invitation_already_used → fix_input (keeps the heuristic). Unlike
#     expired/revoked, "already used" is the benign double-submit case (the token
#     was consumed, commonly by the invitee themselves, who is now a member).
#     There is nothing to escalate and retrying loops on a spent token; fix_input
#     ("the request can't proceed, nothing more to do here") is the honest signal.
#   * 400 workspace_slug_taken → fix_input. Genuine uniqueness conflict on
#     user-chosen input — the user picks a different slug. Matches the heuristic
#     (declared for completeness so every key's canon is explicit).
#   * 400 already_workspace_member → fix_input. The invited user is already in
#     the workspace; the add is a no-op the caller should not repeat. Matches the
#     heuristic.
#   * 400 invalid_role → fix_input. Genuine bad-input (unknown role value from
#     the request body). Matches the heuristic.
WORKSPACES_REMEDIATION = {
    ERR_404_WORKSPACE_NOT_FOUND: "fix_input",
    ERR_404_MEMBER_NOT_FOUND: "fix_input",
    ERR_404_INVITATION_NOT_FOUND: "fix_input",
    ERR_403_FORBIDDEN_WORKSPACE: "contact_support",
    ERR_403_LAST_OWNER: "fix_input",
    ERR_400_SLUG_TAKEN: "fix_input",
    ERR_400_ALREADY_MEMBER: "fix_input",
    ERR_400_INVITATION_EXPIRED: "contact_support",
    ERR_400_INVITATION_ALREADY_USED: "fix_input",
    ERR_400_INVITATION_REVOKED: "contact_support",
    ERR_400_INVALID_ROLE: "fix_input",
}

register_service_errors(WORKSPACES_ERRORS, remediation=WORKSPACES_REMEDIATION)


class WorkspacesErrorKeysView(ErrorKeysView):
    def get_service_errors(self):
        return WORKSPACES_ERRORS
