"""Service-layer helpers for workspaces (creation, invites, provision, suspension)."""

import logging
import uuid
from datetime import timedelta
from secrets import token_urlsafe

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify

from stapel_core.comm import call, emit
from stapel_core.comm.exceptions import (
    FunctionCallError,
    FunctionNotRegistered,
    FunctionRouteNotConfigured,
)
from stapel_core.django.workspaces import invalidate_membership_cache
from stapel_core.signals import workspace_member_changed

from .conf import workspaces_settings
from .dto import WorkspaceSecuritySettings
from .entitlements import (
    ENT_MEMBERS_MAX,
    EntitlementDenied,
    check_org_entitlement,
    debit_provision_credits,
    member_seats_quantity,
)
from .events import (
    EVENT_WORKSPACE_MEMBER_PROVISIONED,
    EVENT_WORKSPACE_MEMBER_SUSPENDED,
    EVENT_WORKSPACE_MEMBER_UNSUSPENDED,
    EVENT_WORKSPACE_PERSONAL_CREATED,
)
from .models import (
    SUSPENSION_ACCOUNT_DEACTIVATED,
    SUSPENSION_NO_MFA,
    Role,
    Workspace,
    WorkspaceInvitation,
    WorkspaceMember,
    WorkspaceType,
)

logger = logging.getLogger(__name__)


def _make_unique_slug(name: str) -> str:
    base = slugify(name)[:48] or "workspace"
    candidate = base
    n = 1
    while Workspace.objects.filter(slug=candidate).exists():
        n += 1
        candidate = f"{base}-{n}"
    return candidate


@transaction.atomic
def create_workspace(*, user, name: str, slug: str | None = None, type: str = WorkspaceType.WORK) -> Workspace:
    """Create a workspace and seed the owner membership."""
    if not slug:
        slug = _make_unique_slug(name)
    ws = Workspace.objects.create(name=name, slug=slug, type=type, owner=user)
    WorkspaceMember.objects.create(
        workspace=ws, user=user, role=Role.OWNER, accepted_at=timezone.now()
    )
    # Transactional outbox: leaves iff this transaction commits.
    emit(
        "workspace.created",
        {
            "workspace_id": str(ws.id),
            "owner_id": str(user.pk),
            "name": ws.name,
            "type": ws.type,
        },
    )
    workspace_member_changed.send(
        sender=WorkspaceMember, workspace=ws, user=user, role=Role.OWNER, action="added"
    )
    return ws


def ensure_personal_workspace(user) -> Workspace:
    """Auto-create a Personal workspace on first login if one doesn't exist."""
    existing = Workspace.objects.filter(
        owner=user, type=WorkspaceType.PERSONAL, deleted_at__isnull=True
    ).first()
    if existing:
        return existing
    ws = create_workspace(user=user, name="Personal", type=WorkspaceType.PERSONAL)
    emit(
        EVENT_WORKSPACE_PERSONAL_CREATED,
        {"workspace_id": str(ws.id), "user_id": str(user.pk)},
    )
    emit(
        "workspace.member_joined",
        {"workspace_id": str(ws.id), "user_id": str(user.pk), "role": str(Role.OWNER)},
    )
    return ws


def create_invitation(*, workspace: Workspace, email: str, role: str, invited_by) -> WorkspaceInvitation:
    invitation = WorkspaceInvitation.objects.create(
        workspace=workspace,
        email=email.lower().strip(),
        role=role,
        invited_by=invited_by,
        token=token_urlsafe(32),
        expires_at=timezone.now()
        + timedelta(days=workspaces_settings.INVITATION_TTL_DAYS),
    )
    _send_invitation_notification(invitation)
    return invitation


def _frontend_url(path: str) -> str:
    """Absolute frontend URL for *path* (FRONTEND_URL flat-setting canon)."""
    frontend_url = (getattr(settings, "FRONTEND_URL", "") or "").rstrip("/")
    return f"{frontend_url}{path}" if frontend_url else path


def _send_invitation_notification(invitation: WorkspaceInvitation) -> None:
    """Ask stapel-notifications to deliver the invite email.

    Best-effort: a delivery hiccup must never break invitation creation —
    the invite stays listable/resendable either way.
    """
    try:
        from django.contrib.auth import get_user_model
        from stapel_core.notifications import request_notification

        User = get_user_model()

        # Canonical frontend invite route (org-program spec §B1): the pair's
        # InviteAcceptFlow lives at /invite/{token}. FRONTEND_URL is the
        # established flat-setting canon for the frontend base URL here.
        accept_url = _frontend_url(f"/invite/{invitation.token}")

        inviter = invitation.invited_by
        inviter_name = ""
        if inviter is not None:
            inviter_name = (
                (inviter.get_full_name() or "").strip()
                or inviter.username
                or inviter.email
                or ""
            )

        invitee = User.objects.filter(email__iexact=invitation.email).first()
        target = (
            {"user_id": str(invitee.pk)}
            if invitee is not None
            else {"email": invitation.email}
        )
        request_notification(
            "workspace.invitation",
            variables={
                "workspace_name": invitation.workspace.name,
                "inviter_name": inviter_name,
                "accept_url": accept_url,
            },
            source_service="workspaces",
            **target,
        )
    except Exception:
        logger.exception(
            "failed to request invitation notification for %s", invitation.pk
        )


#: comm Function owned by stapel-auth (>= 0.11): mints a single-use login
#: grant token bound to an email (org-program spec §B3).
ISSUE_LOGIN_GRANT = "auth.issue_login_grant"


def issue_invitation_login_grant(
    *, invitation: WorkspaceInvitation, language: str | None = None
) -> str:
    """Mint a login grant for a not-yet-registered invitee (claim step).

    Calls ``auth.issue_login_grant`` with ``create_if_missing`` — the
    verified account materializes when the holder exchanges the grant at
    auth's ``/grant/exchange/``. The invitation is deliberately NOT
    consumed: accept stays a separate, conscious step after account setup.

    comm wiring errors (``FunctionNotRegistered`` /
    ``FunctionRouteNotConfigured``) propagate to the caller — an invite
    flow without auth is meaningless, so the view degrades to 503, never
    to allow. The returned token is a credential: never log it.
    """
    payload: dict = {
        "email": invitation.email,
        "verified_email": True,
        "create_if_missing": True,
    }
    if language:
        payload["language"] = language
    result = call(ISSUE_LOGIN_GRANT, payload) or {}
    return result["grant_token"]


@transaction.atomic
def decline_invitation(*, invitation: WorkspaceInvitation, user) -> WorkspaceInvitation:
    """Mark a pending invitation as declined by the invitee.

    Decline ≠ revoke: this is the invitee's terminal "no" (the workspace's
    withdrawal is ``revoked_at``). Same row-lock discipline as accept — a
    single-use token must not race its own state transitions.
    """
    locked = (
        WorkspaceInvitation.objects.select_for_update()
        .unresolved()
        .filter(pk=invitation.pk)
        .first()
    )
    if locked is None:
        raise ValueError("invitation is not pending")
    locked.declined_at = timezone.now()
    locked.save(update_fields=["declined_at"])
    return locked


@transaction.atomic
def accept_invitation(*, invitation: WorkspaceInvitation, user) -> WorkspaceMember:
    # Lock the invitation row: a single-use token must not be consumable
    # twice by concurrent requests. The compare-and-set is the same
    # unresolved() as decline's — hand-written, it had lost the revoked_at
    # clause, so a revocation committing between the view's state check and
    # this lock lost the race and the invite was accepted anyway (0.10.0).
    locked = (
        WorkspaceInvitation.objects.select_for_update()
        .unresolved()
        .filter(pk=invitation.pk)
        .first()
    )
    if locked is None:
        raise ValueError("invitation already used")
    # Entitlement seam (spec §D2): the seat ceiling is re-checked on accept —
    # the org's plan may have changed since the invite went out. The invite
    # itself is still pending here, i.e. already counted in the seat total
    # (additional=0). Re-accepting an existing membership adds no seat and
    # is never blocked. Degrades to allow without billing installed.
    already_member = WorkspaceMember.objects.filter(
        workspace_id=locked.workspace_id, user=user
    ).exists()
    if not already_member:
        verdict = check_org_entitlement(
            locked.workspace,
            ENT_MEMBERS_MAX,
            quantity=member_seats_quantity(locked.workspace),
        )
        if not verdict.allowed:
            raise EntitlementDenied(ENT_MEMBERS_MAX, verdict)
    locked.accepted_at = timezone.now()
    locked.save(update_fields=["accepted_at"])
    member, _ = WorkspaceMember.objects.get_or_create(
        workspace=locked.workspace,
        user=user,
        defaults={"role": locked.role, "accepted_at": timezone.now()},
    )
    # Subscribers must be idempotent (at-least-once delivery), so emitting
    # again for an already-existing membership is safe.
    emit(
        "workspace.member_joined",
        {
            "workspace_id": str(locked.workspace_id),
            "user_id": str(user.pk),
            "role": str(member.role),
        },
    )
    # A negative membership lookup may be cached cross-service; drop it.
    invalidate_membership_cache(locked.workspace_id, user.pk)
    workspace_member_changed.send(
        sender=WorkspaceMember,
        workspace=locked.workspace,
        user=user,
        role=member.role,
        action="added",
    )
    return member


# ---------------------------------------------------------------------------
# Security hardening (org-program spec §C1-C3, Wave 3)
# ---------------------------------------------------------------------------

#: comm Functions owned by stapel-auth (>= 0.12).
PROVISION_USER = "auth.provision_user"
MFA_STATUS = "auth.mfa_status"


class ProvisionError(Exception):
    """auth.provision_user answered with a structured failure.

    ``error_key`` is auth's canonical error key, passed through verbatim to
    the HTTP caller (``error.409.username_taken`` /
    ``error.400.username_namespace_invalid`` / ``error.400.bad_request``) —
    the view derives the HTTP status from the key itself.
    """

    def __init__(self, error_key: str):
        self.error_key = error_key
        super().__init__(error_key)


def security_settings_for(workspace: Workspace) -> WorkspaceSecuritySettings:
    """Typed view of ``Workspace.settings["security"]`` (safe defaults)."""
    return WorkspaceSecuritySettings.from_settings(workspace.settings)


def provision_member(
    *,
    workspace: Workspace,
    username_local: str,
    role: str,
    provisioned_by,
    password: str | None = None,
    display_name: str | None = None,
    email: str | None = None,
):
    """Create an org-provisioned (synthetic) member (org-program spec §C1).

    Flow: billing debit (when ``PROVISION_USER_CREDITS`` > 0; deterministic
    idempotency key per provision attempt) → ``auth.provision_user`` (the
    account materializes in auth with the workspace's first-login policy) →
    ``WorkspaceMember(accepted_at=now, provisioned=True)`` + the
    ``workspace.member_provisioned`` emit → credentials delivery (below).

    The debit deliberately precedes the auth call (spec §C1/§D2 order); a
    provision that then fails at auth (e.g. lost the username race) leaves
    the charge standing — it is visible in billing under
    ``metadata.action = workspaces.provision_user`` with the provision UUID
    in the idempotency key for manual adjustment. The common input errors
    (malformed local part, bad role) are rejected by the serializer BEFORE
    any charge.

    Credentials delivery (the email nuance, spec §C1): a synthetic account
    normally has NO email — there is nowhere to send the
    ``workspace.provisioned_account`` letter, so it is skipped and a
    server-generated password is returned to the provisioning admin in the
    API response, exactly once (``generated_password``). When the optional
    ``email`` IS passed, the letter is sent there too (username +
    ``initial_password`` when generated + login URL). The generated
    password is always present in the return value when the server
    generated one — the admin/org owns the password until first login
    (forced change / MFA enroll). It never rides any event payload and is
    never logged.

    Returns ``(member, username, generated_password | None)``.
    Raises :class:`ProvisionError` on a structured auth failure and lets
    comm wiring errors (auth not installed/routed) propagate — the view
    maps them to 503, this seam never degrades to allow.
    """
    from django.contrib.auth import get_user_model

    username = f"{workspace.slug}/{username_local}"
    provision_id = uuid.uuid4()

    credits = int(workspaces_settings.PROVISION_USER_CREDITS or 0)
    if credits > 0:
        debit_provision_credits(
            workspace,
            provision_id=provision_id,
            username=username,
            credits=credits,
        )

    payload: dict = {
        "username": username,
        "first_login_policy": security_settings_for(
            workspace
        ).provisioned_user_policy,
    }
    if password:
        payload["password"] = password
    if display_name:
        payload["display_name"] = display_name
    if email:
        payload["email"] = email
    result = call(PROVISION_USER, payload) or {}
    if result.get("error"):
        raise ProvisionError(result["error"])

    user_id = result["user_id"]
    generated_password = result.get("generated_password")

    with transaction.atomic():
        member = WorkspaceMember.objects.create(
            workspace=workspace,
            user_id=user_id,
            role=role,
            invited_by=provisioned_by,
            accepted_at=timezone.now(),
            provisioned=True,
        )
        # Transactional outbox: leaves iff this transaction commits. The
        # audit/metering signal for the provisioning action — no
        # credential material ever rides it.
        emit(
            EVENT_WORKSPACE_MEMBER_PROVISIONED,
            {
                "workspace_id": str(workspace.id),
                "user_id": str(user_id),
                "role": str(role),
                "provisioned_by": str(provisioned_by.pk),
            },
        )
    # A negative membership lookup may be cached cross-service; drop it.
    invalidate_membership_cache(workspace.id, user_id)
    user = get_user_model().objects.filter(pk=user_id).first()
    if user is not None:
        workspace_member_changed.send(
            sender=WorkspaceMember,
            workspace=workspace,
            user=user,
            role=role,
            action="added",
        )
    if email:
        _send_provisioned_account_notification(
            workspace=workspace,
            username=username,
            email=email,
            initial_password=generated_password,
        )
    return member, username, generated_password


def _send_provisioned_account_notification(
    *, workspace: Workspace, username: str, email: str, initial_password: str | None
) -> None:
    """Deliver the workspace.provisioned_account credentials letter.

    Only called when the provisioning request carried an email anchor (the
    account is targeted by that address directly — the user has no contact
    records anywhere yet). ``initial_password`` is embedded only when the
    server generated it (the otp_code secret-in-email precedent); an
    admin-chosen password is communicated out-of-band by the admin.
    Best-effort, like every notification here: a delivery hiccup must not
    roll back a provisioned account.
    """
    try:
        from stapel_core.notifications import request_notification

        variables = {
            "workspace_name": workspace.name,
            "username": username,
            "login_url": _frontend_url("/login"),
        }
        if initial_password:
            variables["initial_password"] = initial_password
        request_notification(
            "workspace.provisioned_account",
            variables=variables,
            source_service="workspaces",
            email=email,
        )
    except Exception:
        logger.exception(
            "failed to request provisioned-account notification for %s in %s",
            username.partition("/")[0],
            workspace.pk,
        )


def suspend_member(
    member: WorkspaceMember, *, reason: str, notify: bool = True
) -> bool:
    """Suspend a membership (org-program spec §C3). Idempotent.

    Not removal: the row and the role stay, but the membership stops
    counting for every access check (permissions/functions/internal API all
    filter on ``suspended_at IS NULL``). Emits
    ``workspace.member_suspended``, drops the cross-service membership
    cache entry and (for the canonical ``no_mfa`` reason, unless ``notify``
    is off) sends the ``workspace.mfa_suspension`` letter.

    Returns True when the member was suspended by this call, False for an
    already-suspended member (at-least-once event delivery safe).
    """
    if member.suspended_at is not None:
        return False
    member.suspended_at = timezone.now()
    member.suspension_reason = reason
    with transaction.atomic():
        member.save(update_fields=["suspended_at", "suspension_reason"])
        # Transactional outbox: leaves iff this transaction commits. The
        # cross-service "revoke live access" signal, like member_removed.
        emit(
            EVENT_WORKSPACE_MEMBER_SUSPENDED,
            {
                "workspace_id": str(member.workspace_id),
                "user_id": str(member.user_id),
                "role": str(member.role),
                "reason": reason,
            },
        )
    # Other services cache membership lookups — drop the now-stale entry.
    invalidate_membership_cache(member.workspace_id, member.user_id)
    workspace_member_changed.send(
        sender=WorkspaceMember,
        workspace=member.workspace,
        user=member.user,
        role=member.role,
        action="suspended",
    )
    if notify and reason == SUSPENSION_NO_MFA:
        _send_mfa_notification(
            member,
            "workspace.mfa_suspension",
            {
                "workspace_name": member.workspace.name,
                "security_url": _frontend_url("/settings/security"),
            },
        )
    return True


def unsuspend_member(member: WorkspaceMember, *, notify: bool = True) -> bool:
    """Lift a membership suspension (org-program spec §C3). Idempotent.

    Emits ``workspace.member_unsuspended`` (with the lifted reason), drops
    the membership cache entry and (for the ``no_mfa`` reason, unless
    ``notify`` is off — the policy-off sweep suppresses it: its wording is
    "you enabled 2FA", wrong for that path) sends the
    ``workspace.mfa_restored`` letter.
    """
    if member.suspended_at is None:
        return False
    lifted_reason = member.suspension_reason
    member.suspended_at = None
    member.suspension_reason = ""
    with transaction.atomic():
        member.save(update_fields=["suspended_at", "suspension_reason"])
        emit(
            EVENT_WORKSPACE_MEMBER_UNSUSPENDED,
            {
                "workspace_id": str(member.workspace_id),
                "user_id": str(member.user_id),
                "role": str(member.role),
                "reason": lifted_reason,
            },
        )
    invalidate_membership_cache(member.workspace_id, member.user_id)
    workspace_member_changed.send(
        sender=WorkspaceMember,
        workspace=member.workspace,
        user=member.user,
        role=member.role,
        action="unsuspended",
    )
    if notify and lifted_reason == SUSPENSION_NO_MFA:
        _send_mfa_notification(
            member,
            "workspace.mfa_restored",
            {
                "workspace_name": member.workspace.name,
                "workspace_url": _frontend_url(
                    f"/workspaces/{member.workspace.slug}"
                ),
            },
        )
    return True


def _send_mfa_notification(
    member: WorkspaceMember, notification_type: str, variables: dict
) -> None:
    """Best-effort mfa_suspension / mfa_restored letter to the member."""
    try:
        from stapel_core.notifications import request_notification

        request_notification(
            notification_type,
            user_id=str(member.user_id),
            variables=variables,
            source_service="workspaces",
        )
    except Exception:
        logger.exception(
            "failed to request %s notification for member %s",
            notification_type,
            member.pk,
        )


def enforce_require_mfa(workspace: Workspace) -> bool:
    """Sync sweep when the require_mfa policy flips on (spec §C3).

    Asks ``auth.mfa_status`` for every active (accepted, non-suspended)
    member and suspends those without a strong second factor (reason
    ``no_mfa``, emit + letter). Fail-open by suspension: when auth is
    unavailable (not wired, or a call fails) members are NOT touched — the
    sweep stops and returns False; fail-closed would lock out the whole
    org on an auth hiccup. The ``user.mfa_disabled`` consumer catches up
    once auth events flow again.
    """
    members = (
        WorkspaceMember.objects.active()
        .filter(workspace=workspace)
        .select_related("workspace", "user")
        .order_by("invited_at")
    )
    for member in members:
        try:
            status = call(MFA_STATUS, {"user_id": str(member.user_id)}) or {}
        except (
            FunctionNotRegistered,
            FunctionRouteNotConfigured,
            FunctionCallError,
        ) as exc:
            logger.warning(
                "auth.mfa_status unavailable (%s) — require_mfa sweep for "
                "workspace %s aborted, remaining members untouched (fail-open)",
                exc,
                workspace.pk,
            )
            return False
        if not status.get("has_strong_mfa"):
            suspend_member(member, reason=SUSPENSION_NO_MFA)
    return True


def lift_no_mfa_suspensions(workspace: Workspace) -> int:
    """Lift every ``no_mfa`` suspension when require_mfa flips off.

    A policy the org no longer enforces must not keep members locked out
    (their only other exit is enabling MFA). Emits per member; the
    mfa_restored letter is suppressed (its wording is about the USER
    enabling 2FA, not the org dropping the policy).
    """
    lifted = 0
    members = (
        WorkspaceMember.objects.suspended(reason=SUSPENSION_NO_MFA)
        .filter(workspace=workspace)
        .select_related("workspace", "user")
    )
    for member in members:
        if unsuspend_member(member, notify=False):
            lifted += 1
    return lifted


def suspend_memberships_without_mfa(user_id) -> int:
    """``user.mfa_disabled`` consumer body: the user lost their last strong
    factor — suspend their memberships in every require_mfa workspace.

    Idempotent (at-least-once delivery): already-suspended memberships are
    filtered out, a redelivery is a no-op.
    """
    suspended = 0
    members = (
        WorkspaceMember.objects.active()
        .filter(user_id=user_id)
        .select_related("workspace", "user")
    )
    for member in members:
        workspace = member.workspace
        if workspace.deleted_at:
            continue
        if security_settings_for(workspace).require_mfa:
            if suspend_member(member, reason=SUSPENSION_NO_MFA):
                suspended += 1
    return suspended


def lift_no_mfa_suspensions_for_user(user_id) -> int:
    """``user.mfa_enabled`` consumer body: the user gained a strong factor —
    lift their ``no_mfa`` suspensions (ONLY that reason; suspensions for
    other/future reasons are none of MFA's business). Idempotent.
    """
    lifted = 0
    members = (
        WorkspaceMember.objects.suspended(reason=SUSPENSION_NO_MFA)
        .filter(user_id=user_id)
        .select_related("workspace", "user")
    )
    for member in members:
        if unsuspend_member(member):
            lifted += 1
    return lifted


def suspend_memberships_for_deactivated_user(user_id) -> int:
    """``user.deactivated`` consumer body (#92): the ACCOUNT was
    administratively deactivated in auth — suspend every membership it holds.

    Unlike the MFA consumer this is not policy-conditional: no workspace
    setting can make a deactivated account admissible, so every accepted
    membership goes, whatever the workspace's security settings say. The
    same suspension mechanism as everywhere else (``suspended_at`` +
    reason), NOT a second way to switch a membership off — the row, the
    role and the history stay, and :func:`lift_deactivation_suspensions_for_user`
    puts them back verbatim.

    Emphatically not deletion: the GDPR erasure path
    (``user.deleted`` → :meth:`WorkspacesGDPRProvider.delete`) removes rows
    and is a different event with different, irreversible consequences.

    Idempotent (at-least-once delivery): a member already suspended for any
    reason is filtered out, so a redelivery neither fails nor overwrites the
    original ``suspended_at``/``suspension_reason``. That also means a
    membership already suspended for ``no_mfa`` keeps that reason — and
    stays suspended after reactivation, which is correct: the MFA gap did
    not go away because the account came back.

    Soft-deleted workspaces are skipped, as in the MFA consumer: there is no
    access left to revoke there, and touching them would emit noise.

    Returns the number of memberships suspended by this call.
    """
    suspended = 0
    members = (
        WorkspaceMember.objects.active()
        .filter(user_id=user_id)
        .select_related("workspace", "user")
    )
    for member in members:
        if member.workspace.deleted_at:
            continue
        # notify=False: the account has just lost every way in — a per
        # workspace letter to an address the owner can no longer act on is
        # noise, and suspend_member only writes the MFA-worded one anyway.
        if suspend_member(
            member, reason=SUSPENSION_ACCOUNT_DEACTIVATED, notify=False
        ):
            suspended += 1
    return suspended


def lift_deactivation_suspensions_for_user(user_id) -> int:
    """``user.reactivated`` consumer body (#92): the account is admitted
    again — lift the suspensions THIS module put on for it.

    Only ``account_deactivated`` ones. A ``no_mfa`` suspension is the MFA
    consumer's to lift and stays put, or restoring an account would silently
    walk an MFA-less user back into a require_mfa workspace.

    Without this the deactivation half is a trap: the user logs back in
    (the session guard admits them again) and sees an empty product.
    Idempotent — an already-active membership is filtered out.
    """
    lifted = 0
    members = (
        WorkspaceMember.objects.suspended(reason=SUSPENSION_ACCOUNT_DEACTIVATED)
        .filter(user_id=user_id)
        .select_related("workspace", "user")
    )
    for member in members:
        if unsuspend_member(member, notify=False):
            lifted += 1
    return lifted
