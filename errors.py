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
ERR_400_INVITATION_DECLINED = "error.400.invitation_declined"
ERR_403_MISSING_CAPABILITY = "error.403.missing_capability"
ERR_402_ENTITLEMENT_REQUIRED = "error.402.entitlement_required"
ERR_402_MEMBER_LIMIT_REACHED = "error.402.member_limit_reached"
ERR_409_EMAIL_ALREADY_REGISTERED = "error.409.email_already_registered"
ERR_503_AUTH_UNAVAILABLE = "error.503.auth_unavailable"
ERR_403_MEMBERSHIP_SUSPENDED = "error.403.membership_suspended"
ERR_400_INVALID_PROVISION_USERNAME = "error.400.invalid_provision_username"
ERR_403_ROLE_EXCEEDS_INVITER_RANK = "error.403.role_exceeds_inviter_rank"
ERR_503_PROFILES_UNAVAILABLE = "error.503.profiles_unavailable"

# Display-name keys BORROWED from stapel-profiles, not minted here.
#
# The roster's two name-edit PATCHes write a name, and the canon for what a
# name may contain belongs to stapel-profiles alone
# (`validators.validate_display_name`, its docs/llms.txt: "any host
# onboarding form, admin action or importer that writes a name must run it
# through here instead of inventing a second, differently-strict regex").
# This module calls that validator (services.display_name_canon) and lets
# its refusals out verbatim — same string keys, same English, same
# remediation — so a frontend branches on ONE set of display-name codes no
# matter which service refused the write.
#
# They are re-declared here only so this module's contract artifact
# (docs/errors.json) is honest about what its own endpoints can answer with:
# the registry is a last-wins global dict, so a deployment running both
# modules registers identical entries twice and nothing drifts. Adding a
# fifth, workspaces-only display-name rule here would be the defect these
# comments exist to prevent — the length ceiling is enforced as the
# serializer field's `max_length` (error.400.field.max_length), which is a
# storage fact both models already declare, not a second name canon.
ERR_400_DISPLAY_NAME_TOO_SHORT = "error.400.display_name_too_short"
ERR_400_DISPLAY_NAME_FORBIDDEN_CHARS = "error.400.display_name_forbidden_chars"
ERR_400_DISPLAY_NAME_EMOJI = "error.400.display_name_emoji"
ERR_400_DISPLAY_NAME_INVISIBLE_CHARS = "error.400.display_name_invisible_chars"

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
    ERR_400_INVITATION_DECLINED: "Invitation has been declined",
    ERR_400_INVALID_ROLE: "Invalid role",
    ERR_403_MISSING_CAPABILITY: "Your role does not include the {capability} capability in this workspace",
    ERR_402_ENTITLEMENT_REQUIRED: "The workspace owner's plan does not include this feature",
    ERR_402_MEMBER_LIMIT_REACHED: "The workspace member limit ({limit}) has been reached",
    ERR_409_EMAIL_ALREADY_REGISTERED: "An account with this email already exists — log in instead",
    ERR_503_AUTH_UNAVAILABLE: "The authentication service is unavailable; try again later",
    ERR_403_MEMBERSHIP_SUSPENDED: "Your membership in this workspace is suspended ({reason})",
    ERR_400_INVALID_PROVISION_USERNAME: "Invalid username for a provisioned account",
    ERR_403_ROLE_EXCEEDS_INVITER_RANK: "You cannot grant a role that outranks your own ({role})",
    ERR_503_PROFILES_UNAVAILABLE: "The profiles service is unavailable; try again later",
    # Verbatim from stapel-profiles' own registry — see the note above.
    ERR_400_DISPLAY_NAME_TOO_SHORT: "Display name must be at least 2 characters",
    ERR_400_DISPLAY_NAME_FORBIDDEN_CHARS: "Display name contains forbidden characters",
    ERR_400_DISPLAY_NAME_EMOJI: "Display name cannot contain emoji",
    ERR_400_DISPLAY_NAME_INVISIBLE_CHARS: "Display name contains invisible characters",
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
#   * 403 missing_capability → contact_support. Same boundary as
#     forbidden_workspace, one level deeper: the user IS a member but their
#     role lacks the capability. No request field grants it; a workspace
#     admin/owner must change their role — escalate to another party.
#   * 402 entitlement_required / member_limit_reached → fix_input. Self-serve
#     plan ceilings: the workspace owner upgrades the plan (or prunes
#     members/invites). Precedent: billing's insufficient_credits →
#     fix_input; no operator involvement, retrying loops.
#   * 400 invitation_declined → contact_support. Same shape as
#     expired/revoked: the token is terminally dead (the invitee said no);
#     the only recovery is a fresh invitation from the workspace — an
#     external party. No field to fix, retrying loops.
#   * 409 email_already_registered → reauthenticate. The claim path is for
#     unregistered emails only; an existing account means the honest
#     recovery is logging into it (the frontend switches to the login
#     screen) — exactly what reauthenticate signals.
#   * 503 auth_unavailable → wait_and_retry. Transient wiring/deploy gap
#     (auth's login-grant Function not reachable); the request itself is
#     fine and succeeds once auth is back.
#   * 403 membership_suspended → fix_input. Same shape as last_owner: a
#     self-serve precondition, not an authorization wall. The canonical
#     reason (no_mfa) states its own fix — enable a strong second factor —
#     and access restores AUTOMATICALLY (the mfa_enabled consumer lifts the
#     suspension; the mfa_suspension email says "no need to contact
#     anyone"). Retrying loops; contact_support is wrong for the canonical
#     reason (no operator involved). Future non-self-serve reasons can
#     revisit per-reason.
#   * 400 invalid_provision_username → fix_input. Genuine bad-input: the
#     local username part fails the stock username canon (or smuggles a
#     '/'); the admin picks a different local name. Matches the heuristic.
#   * 403 role_exceeds_inviter_rank → fix_input. A self-serve precondition,
#     not an authorization wall the caller cannot influence: the request
#     names a role rank above the actor's own, and the fix is entirely
#     within the caller's own request — pick a role at or below their rank.
#     Retrying the identical request loops; no other party need be involved
#     (matches the last_owner_cannot_be_removed precedent, not
#     forbidden_workspace/missing_capability's contact_support).
#   * 503 profiles_unavailable → wait_and_retry, the same shape as
#     auth_unavailable: the request itself is fine and succeeds once the
#     module that OWNS display names is reachable in this deployment. It is
#     a wiring gap (stapel-profiles not installed in this process), not
#     anything the caller can edit.
#   * 400 display_name_* → fix_input, verbatim from stapel-profiles'
#     declarations. Same key, same hint, on purpose: a frontend that already
#     highlights the name field on profiles' refusal must behave identically
#     when the refusal came from the roster instead.
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
    ERR_403_MISSING_CAPABILITY: "contact_support",
    ERR_402_ENTITLEMENT_REQUIRED: "fix_input",
    ERR_402_MEMBER_LIMIT_REACHED: "fix_input",
    ERR_400_INVITATION_DECLINED: "contact_support",
    ERR_409_EMAIL_ALREADY_REGISTERED: "reauthenticate",
    ERR_503_AUTH_UNAVAILABLE: "wait_and_retry",
    ERR_403_MEMBERSHIP_SUSPENDED: "fix_input",
    ERR_400_INVALID_PROVISION_USERNAME: "fix_input",
    ERR_403_ROLE_EXCEEDS_INVITER_RANK: "fix_input",
    ERR_503_PROFILES_UNAVAILABLE: "wait_and_retry",
    ERR_400_DISPLAY_NAME_TOO_SHORT: "fix_input",
    ERR_400_DISPLAY_NAME_FORBIDDEN_CHARS: "fix_input",
    ERR_400_DISPLAY_NAME_EMOJI: "fix_input",
    ERR_400_DISPLAY_NAME_INVISIBLE_CHARS: "fix_input",
}

register_service_errors(WORKSPACES_ERRORS, remediation=WORKSPACES_REMEDIATION)


class WorkspacesErrorKeysView(ErrorKeysView):
    def get_service_errors(self):
        return WORKSPACES_ERRORS
