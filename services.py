"""Service-layer helpers for workspaces (creation, invites, provision, suspension)."""

import logging
import os
import uuid
from datetime import timedelta
from secrets import token_urlsafe

import requests
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
from stapel_core.django.peers import service_answered
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
    EVENT_WORKSPACE_INVITATION_REVOKED,
    EVENT_WORKSPACE_MEMBER_PASSWORD_RESET,
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


def resolve_landing_workspace(user, *, origin: str = "street") -> Workspace | None:
    """Canon landing-mandate policy for a freshly (re)appearing account.

    Org-program #85 (mandate-model vardict, 2026-08-03): before this, the
    ONLY caller-facing primitive was :func:`ensure_personal_workspace`,
    which is unconditional — every product subscriber to ``user.registered``
    that called it made "become OWNER of a personal workspace" the
    inescapable fate of every signup. That is a product policy wearing a
    library function's clothes: this canon is the seam that lets a host
    CHOOSE the policy instead of forking the subscriber.

    ``origin`` describes what the CALLER already knows about how the
    account got here:

    * ``"invited"`` — the account's membership was (or will be) created by
      a *separate* mechanism, :func:`accept_invitation`. This canon is a
      deliberate no-op for that origin: acting here too would either
      duplicate the invite's own membership or race it. Returns ``None``.
    * anything else (``"street"``, ``"anon"``, ...) — no invitation context;
      the account showed up on its own. Governed by the
      ``STREET_LANDING_MODE`` setting (default ``"personal"``):

      - ``"personal"``: :func:`ensure_personal_workspace` — the historical
        behavior, preserved byte-for-byte so an existing deployment that
        never touches the setting sees no change at all.
      - ``"none"``: no workspace is created; the account is a guest per
        :func:`~stapel_workspaces.permissions.is_guest` until somebody
        invites it. This is the closed-organization shape from the owner's
        mandate-model decision — a host opts in explicitly, it is never the
        default for an existing install.

    Any other value of ``STREET_LANDING_MODE`` is treated as ``"none"``
    (fail toward NOT minting an unrequested workspace, the safer side of an
    admin typo — the opposite failure, silently making everyone an owner
    again, is exactly what #85 exists to stop being the default).
    """
    if origin == "invited":
        return None
    mode = workspaces_settings.STREET_LANDING_MODE
    if mode == "personal":
        return ensure_personal_workspace(user)
    return None


# ---------------------------------------------------------------------------
# stapel-profiles integration (read-only, best-effort, HTTP — not an import)
# ---------------------------------------------------------------------------
#
# This module's own convention (MODULE.md: "Stapel modules never import each
# other; all cross-module communication goes through stapel-core") rules out
# ``from stapel_profiles... import ...`` here — same as every other sibling
# seam in this file (auth, notifications, billing), all of which go through
# stapel-core instead of a direct import. stapel-profiles registers no comm
# Function for this (its own docs/capabilities.json surface is HTTP-operation
# only), so the mechanism is a plain HTTP call to its public, AllowAny
# ``POST /profiles/api/v1/batch`` — built by that module for exactly this
# "resolve many ids at once" shape — using stapel-core's own peer-client
# discipline (``service_answered``: a routing 404 is never a verdict) rather
# than reading a network hiccup as "nobody has a name".
#
# PROFILES_SERVICE_URL follows the same flat-setting canon as FRONTEND_URL
# above and stapel-core's WORKSPACES_SERVICE_URL: unset (the default) turns
# the whole integration off, no network attempt is ever made, and every
# caller falls back to WorkspaceMember.display_name_hint alone.
def _profiles_service_url() -> str:
    return (
        getattr(settings, "PROFILES_SERVICE_URL", "")
        or os.environ.get("PROFILES_SERVICE_URL", "")
    ).rstrip("/")


def _fetch_profile_display_names(user_ids) -> dict:
    """Best-effort ``{str(user_id): display_name}`` from stapel-profiles.

    Only ids with a NON-EMPTY name there are present in the result — an id
    with no profile row, or an empty ``display_name``, is simply absent,
    never a placeholder (mirrors ``ProfileBatchResponse``'s own "missing is
    not invented" contract). Callers fall back further to
    ``WorkspaceMember.display_name_hint`` for anything absent here.

    Best-effort like every other optional cross-service seam this module
    already has (auth/notifications/billing): ``PROFILES_SERVICE_URL``
    unset, a transport error, a routing miss (this service and
    stapel-profiles disagree about the path — see ``service_answered``), or
    any non-200 all degrade to ``{}`` rather than raising. A member's name
    is cosmetic; it is never worth failing a roster over.
    """
    ids = list(dict.fromkeys(str(uid) for uid in user_ids))
    if not ids:
        return {}

    # In-process FIRST. This seam was written as if stapel-profiles were
    # always a remote service, so a monolith that has `stapel_profiles` right
    # there in INSTALLED_APPS still got `{}` — PROFILES_SERVICE_URL is unset
    # (nobody points a service at itself), the HTTP branch bails on line one,
    # and the caller silently degrades to an email address. Measured live on
    # meettoday 2026-08-05: profiles installed in the same process, name
    # never found, invitation emails addressed from a bare email — which is
    # why the product had grown its own `profile.changed` subscriber copying
    # display_name into `User.first_name` just to make `get_full_name()` fire.
    # A cross-service seam that cannot see a module sitting next to it is
    # half a seam.
    try:
        from django.apps import apps as _django_apps

        if _django_apps.is_installed("stapel_profiles"):
            Profile = _django_apps.get_model("stapel_profiles", "Profile")
            rows = Profile.objects.filter(user_id__in=ids).values_list(
                "user_id", "display_name"
            )
            local = {str(uid): name for uid, name in rows if (name or "").strip()}
            if local:
                return local
    except Exception:
        # Same best-effort contract as the HTTP branch below: a cosmetic name
        # is never worth failing a roster over.
        logger.warning("in-process stapel-profiles lookup errored", exc_info=True)

    base = _profiles_service_url()
    if not base:
        return {}
    try:
        headers = {"Accept": "application/json"}
        api_key = os.environ.get("SERVICE_API_KEY", "")
        if api_key:
            headers["X-API-KEY"] = api_key
        resp = requests.post(
            f"{base}/profiles/api/v1/batch",
            json={"user_ids": ids},
            headers=headers,
            timeout=3.0,
        )
        if resp.status_code == 404 and not service_answered(resp):
            logger.warning(
                "stapel-profiles batch endpoint not found at %s (routing, "
                "not the view — path skew between this service and "
                "stapel-profiles)",
                base,
            )
            return {}
        if resp.status_code != 200:
            logger.warning(
                "stapel-profiles batch lookup failed: HTTP %s", resp.status_code
            )
            return {}
        payload = resp.json()
        return {
            p["user_id"]: p["display_name"]
            for p in payload.get("profiles", [])
            if p.get("display_name")
        }
    except Exception:
        logger.warning("stapel-profiles batch lookup errored", exc_info=True)
        return {}


def create_invitation(
    *,
    workspace: Workspace,
    email: str,
    role: str,
    invited_by,
    display_name: str | None = None,
) -> WorkspaceInvitation:
    invitation = WorkspaceInvitation.objects.create(
        workspace=workspace,
        email=email.lower().strip(),
        role=role,
        invited_by=invited_by,
        token=token_urlsafe(32),
        expires_at=timezone.now()
        + timedelta(days=workspaces_settings.INVITATION_TTL_DAYS),
        # A NAME HINT (this invite's "Name" field), not the canonical name —
        # see WorkspaceMember.display_name_hint's docstring for why this
        # module stores it at all despite the name living in stapel-profiles.
        display_name_hint=(display_name or "").strip(),
    )
    _send_invitation_notification(invitation)
    return invitation


def _frontend_url(path: str) -> str:
    """Absolute frontend URL for *path* (FRONTEND_URL flat-setting canon)."""
    frontend_url = (getattr(settings, "FRONTEND_URL", "") or "").rstrip("/")
    return f"{frontend_url}{path}" if frontend_url else path


def _send_invitation_notification(
    invitation: WorkspaceInvitation,
    *,
    notification_type: str = "workspace.invitation",
) -> None:
    """Ask stapel-notifications to deliver the invite email.

    Best-effort: a delivery hiccup must never break invitation creation —
    the invite stays listable/resendable either way.

    ``notification_type`` distinguishes the first letter from a re-delivery:
    the resend path passes ``workspace.invitation.reminder`` (its own type
    in the notifications catalog, >= 0.6.1), because "you are being
    reminded — and the earlier link no longer works" is a different message
    from "you are being invited". Same variables either way.
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
            # Canonical name lives in stapel-profiles (same best-effort HTTP
            # batch seam as the member roster's display_name, above) —
            # ``get_full_name()`` is usually empty and ``username`` is a
            # generated login that must never reach a human in an email.
            # Found 2026-08 (owner report): an inviter with no profile name
            # and no first/last name on the User row mailed the invitee a
            # string like "u_8f2a1c" as their name. Drop straight to the
            # email address instead — an address at least identifies who
            # sent it, where a generated login identifies nothing.
            profile_names = _fetch_profile_display_names([inviter.pk])
            inviter_name = (
                profile_names.get(str(inviter.pk))
                or (inviter.get_full_name() or "").strip()
                or inviter.email
                or ""
            )

        invitee = User.objects.filter(email__iexact=invitation.email).first()
        # Always carry the invitation's own address — it is the one thing
        # this function is certain of, and notifications treats an explicit
        # ``email`` as an override over its own contact-table lookup
        # (``recipient_email = email or (contact.email if contact else
        # None)`` in stapel-notifications services.process_notification).
        # ``user_id`` rides ALONGSIDE it (not instead of it) when the
        # invitee already has an account, purely so notifications can apply
        # that account's language/push/preference settings.
        #
        # Before this, a known invitee was targeted by user_id ALONE. That
        # works only when stapel-notifications' own UserContact table
        # happens to have a row for them — it does not look at the
        # account's own email field at all. An invitee who registered
        # before UserContact existed (or via a path that never wrote one)
        # has no row there, so the invite silently produced zero deliverable
        # channels: created (201), logged
        # "no email address for this recipient", nobody notified. Found on
        # the meettoday sandbox 2026-08 by inviting a pre-existing account.
        target = {"email": invitation.email}
        if invitee is not None:
            target["user_id"] = str(invitee.pk)
        request_notification(
            notification_type,
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

    KNOWN GAP (meettoday audit, 2026-08-04), not fixable from this side:
    ``invitation.display_name_hint`` is deliberately NOT forwarded in the
    payload below. auth's ``ISSUE_LOGIN_GRANT_SCHEMA`` (functions.py) has no
    ``display_name`` property, ``LoginGrantService.issue``/the cache payload
    never stores one, and ``LoginGrantService.exchange`` calls
    ``_notify_user_registered(user, language=...)`` WITHOUT a
    ``display_name`` — so even if this call carried the hint, it would be
    silently dropped three times over before ``user.registered`` fires.
    Contrast ``auth.provision_user``, which already threads a
    ``display_name`` through to that same
    ``_notify_user_registered(display_name=...)`` call — this is the exact
    same plumbing, just missing on the grant path. Fixing it is a
    stapel-auth change (add the schema property, store it on the grant,
    pass it through on exchange), out of this module's repo. Until then, an
    invite accepted via the claim flow (a brand-new account) keeps its name
    ONLY as ``WorkspaceMember.display_name_hint`` — never in stapel-profiles
    — whereas an invite accepted by an ALREADY-registered account gets the
    hint on the membership row exactly the same way either way (see
    ``accept_invitation``).
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
def revoke_invitation(
    *, invitation: WorkspaceInvitation, revoked_by
) -> WorkspaceInvitation:
    """Withdraw a live invitation on the workspace's behalf (#109).

    The admin-side terminal transition, the mirror of
    :func:`decline_invitation`: revoke is the *workspace* saying no,
    decline is the *invitee*. Both stay distinguishable in
    ``WorkspaceInvitation.status`` forever, and the freed seat is returned
    to the org immediately — ``pending()`` drives
    :func:`~stapel_workspaces.entitlements.member_seats_quantity`, so a
    revoked row stops being billed the moment this commits.

    Same compare-and-set as accept and decline, and for the same reason
    (0.10.0): the view's state check and this lock are two different reads,
    and the interval between them is exactly where the losing transition
    used to slip through. Re-reading under ``select_for_update().
    unresolved()`` means an accept that committed in that window makes
    ``locked`` None and this raises — the invitee is a member and the
    revocation honestly fails, instead of both "succeeding" and leaving a
    revoked-and-accepted row.

    Raises ``ValueError`` when the invitation is no longer actionable; the
    view maps it to the state error the caller can read.
    """
    locked = (
        WorkspaceInvitation.objects.select_for_update()
        .unresolved()
        .filter(pk=invitation.pk)
        .first()
    )
    if locked is None:
        raise ValueError("invitation is not pending")
    locked.revoked_at = timezone.now()
    locked.save(update_fields=["revoked_at"])
    emit(
        EVENT_WORKSPACE_INVITATION_REVOKED,
        {
            "workspace_id": str(locked.workspace_id),
            "invitation_id": str(locked.pk),
            "role": str(locked.role),
            "revoked_by": str(revoked_by.pk),
        },
    )
    return locked


def resend_invitation(*, invitation: WorkspaceInvitation) -> WorkspaceInvitation:
    """Re-deliver an invitation: rotate the token, extend the TTL, mail it (#109).

    The reason an admin resends is almost always that the first letter
    never arrived or the TTL ran out, so this deliberately accepts an
    EXPIRED invitation: the compare-and-set is the clock-free
    ``unresolved()``, which excludes exactly the three terminal timestamps
    and nothing else. An accepted, declined or revoked invitation is never
    resendable — those are decisions, not delivery failures.

    The token is **rotated**, not reused. It is a bearer secret whose only
    protection is that it stayed in one mailbox; a resend usually means
    nobody knows where the first copy ended up. The fresh letter carries
    the fresh link, and the old one dies with the row it no longer matches.

    ``expires_at`` restarts from now (``INVITATION_TTL_DAYS``) — a resent
    invitation the invitee cannot use before it expires again is not a
    resend. NB: this re-reserves the seat (a revived expired invitation is
    ``pending()`` again); the ceiling is re-checked by the caller, not
    here.
    """
    with transaction.atomic():
        locked = (
            WorkspaceInvitation.objects.select_for_update()
            .unresolved()
            .filter(pk=invitation.pk)
            .first()
        )
        if locked is None:
            raise ValueError("invitation is not pending")
        locked.token = token_urlsafe(32)
        locked.expires_at = timezone.now() + timedelta(
            days=workspaces_settings.INVITATION_TTL_DAYS
        )
        locked.save(update_fields=["token", "expires_at"])
    # Outside the row lock: delivery is best-effort and must not hold a
    # write lock open across a cross-service notification call. A resend is
    # a reminder, not a first invitation — its own notification type, so
    # the letter can say "the earlier link no longer works" honestly.
    _send_invitation_notification(
        locked, notification_type="workspace.invitation.reminder"
    )
    return locked


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
        # display_name_hint only applies on CREATE (get_or_create's
        # defaults never touch an existing row) — accepting an invitation a
        # second time (already_member above) must not clobber whatever name
        # the member already carries. This is the "don't lose the name
        # along the way" step: the hint typed into the invite modal survives all
        # the way to the membership row it becomes.
        defaults={
            "role": locked.role,
            "accepted_at": timezone.now(),
            "display_name_hint": locked.display_name_hint,
        },
    )
    # The org's first-login demands, applied to the joining account (#90).
    # Inside the transaction on purpose: if auth cannot be reached, the
    # whole acceptance rolls back rather than admitting somebody the org
    # said must clear a step first. The seam is only touched when the org
    # CONFIGURED policies — an org that never opened the security screen
    # is not coupled to auth's version by this line at all.
    apply_first_login_policies(
        user_id=user.pk,
        policies=security_settings_for(
            locked.workspace
        ).policies_for_invited_members(),
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

#: comm Function owned by stapel-auth (>= 0.17): raises first-login
#: policies on an EXISTING account (#90).
APPLY_FIRST_LOGIN_POLICIES = "auth.apply_first_login_policies"


def apply_first_login_policies(*, user_id, policies) -> list:
    """Demand the org's first-login steps of an account it just admitted (#90).

    A no-op — and, importantly, NOT a comm call at all — when *policies* is
    empty. That is the ordinary case: an org that never opened the security
    screen configures nothing, so this line does not couple its invitation
    flow to auth's version or availability.

    When the org DID configure policies the call is made and its failures
    are NOT swallowed. ``FunctionNotRegistered`` /
    ``FunctionRouteNotConfigured`` propagate to the caller, which maps them
    to an honest 503 — the org stated a precondition for admission, and a
    seam that cannot honour it must refuse the admission rather than let
    somebody in unhardened. The opposite choice (best-effort, log and
    continue) is the shape every "security control that silently stopped
    running" incident has.

    Returns the policies auth actually raised (a subset: one already
    outstanding, or an ``mfa_enroll`` against an account that already
    carries a strong factor, is not raised again).
    """
    if not policies:
        return []
    result = call(
        APPLY_FIRST_LOGIN_POLICIES,
        {"user_id": str(user_id), "policies": list(policies)},
    ) or {}
    if result.get("error"):
        # auth refused structurally (unknown account, malformed set). The
        # membership must not stand on a precondition that never landed.
        raise ProvisionError(result["error"])
    return result.get("applied") or []


class ProvisionError(Exception):
    """An auth comm Function answered with a structured failure.

    ``error_key`` is auth's canonical error key, passed through verbatim to
    the HTTP caller (``error.409.username_taken`` /
    ``error.400.username_namespace_invalid`` / ``error.400.bad_request`` /
    ``error.404.not_found``) — the view derives the HTTP status from the
    key itself.

    Raised by :func:`provision_member` (``auth.provision_user``) and by
    :func:`apply_first_login_policies`
    (``auth.apply_first_login_policies``, #90). The name is historical; the
    meaning is "auth said no, in its own vocabulary, and the answer is the
    caller's to render".
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
        # A SET of independent demands since auth 0.17 (#90): the old
        # singular `first_login_policy` made the two mutually exclusive at
        # the payload, so an org could not require a password rotation AND
        # a second factor. Nothing downstream ever needed them to be
        # alternatives.
        "username": username,
        "first_login_policies": security_settings_for(
            workspace
        ).provisioned_user_policies,
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
            # Same name-hint treatment as an invitation (see
            # WorkspaceMember.display_name_hint) — the admin already typed
            # this name once for auth.provision_user above; showing it in
            # the member list too costs nothing extra.
            display_name_hint=(display_name or "").strip(),
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


#: comm Function owned by stapel-auth (>= 0.18): resets an account's
#: password on an administrator's order (#110).
ADMIN_RESET_PASSWORD = "auth.admin_reset_password"


def reset_member_password(
    *,
    workspace: Workspace,
    member: WorkspaceMember,
    reset_by,
    password: str | None = None,
    first_login_policies: list | None = None,
    reason: str | None = None,
):
    """Reset a member's password on the organization's order (#110).

    The credential work is auth's (``auth.admin_reset_password``): it
    replaces the password, kills the member's live sessions, raises the
    first-login demands and writes the actor onto its own audit row. This
    function owns the ORG half — which policies, which event, and telling
    the member it happened.

    **Which policies.** Defaults to the workspace's own
    ``provisioned_user_policies`` (#90), which itself defaults to
    ``password_change``. An admin-set password is known to somebody other
    than the account's owner, so it must stop working at its first use;
    an org that wants to suppress that says so with an explicit empty
    list, and that shows up in the audit row rather than in nobody's
    memory.

    **The member is told.** A password reset is exactly the event an
    account-takeover looks like, so it is never silent: a
    ``workspace.member_password_reset`` letter goes to the member,
    naming the workspace and the admin who did it. Best-effort like every
    notification here — a delivery hiccup must not roll back a completed
    credential change, and the return value reports honestly whether a
    channel existed at all.

    The letter deliberately does **not** carry the new password. Unlike a
    provisioned account (which has no other way in and gets its
    credentials mailed), a reset target already exists and may well have a
    self-service recovery path; the admin who ordered the reset holds the
    password and hands it over out of band. The letter's job is the
    security signal — "this happened, and here is who did it" — which is
    worth nothing if the same message also contains the credential.

    Returns ``(generated_password | None, sessions_revoked, policies_applied,
    notified)``. Raises :class:`ProvisionError` on a structured auth
    failure (unknown account, privileged target, rejected password) and
    lets comm wiring errors propagate — the view maps them to 503, this
    seam never degrades to "reported success".
    """
    if first_login_policies is None:
        first_login_policies = security_settings_for(
            workspace
        ).provisioned_user_policies
    payload: dict = {
        "user_id": str(member.user_id),
        "first_login_policies": list(first_login_policies),
        "actor_id": str(reset_by.pk),
    }
    if password:
        payload["password"] = password
    if reason:
        payload["reason"] = reason
    result = call(ADMIN_RESET_PASSWORD, payload) or {}
    if result.get("error"):
        raise ProvisionError(result["error"])

    generated = result.get("generated_password")
    sessions_revoked = int(result.get("sessions_revoked") or 0)
    applied = list(result.get("first_login_policies_applied") or [])

    # Transactional outbox: the org-side audit record of the action. Auth
    # has its own row; this one is what a workspace-scoped activity log
    # reads. No credential material rides it, ever.
    with transaction.atomic():
        emit(
            EVENT_WORKSPACE_MEMBER_PASSWORD_RESET,
            {
                "workspace_id": str(workspace.id),
                "user_id": str(member.user_id),
                "role": str(member.role),
                "reset_by": str(reset_by.pk),
                "sessions_revoked": sessions_revoked,
            },
        )
    notified = _send_password_reset_notification(
        workspace=workspace, member=member, reset_by=reset_by
    )
    return generated, sessions_revoked, applied, notified


def _send_password_reset_notification(
    *, workspace: Workspace, member: WorkspaceMember, reset_by
) -> bool:
    """Tell the member their password was reset, and by whom.

    Targeted by ``user_id`` rather than an address: the notifications
    service owns contact resolution, and an org-provisioned member may
    have no email at all. Returns whether a notification was accepted —
    ``False`` means nobody could be reached and the admin is the only
    channel left.

    Never carries the new password (see :func:`reset_member_password`).
    """
    try:
        from stapel_core.notifications import request_notification

        actor = reset_by
        actor_name = (
            (actor.get_full_name() or "").strip()
            or actor.username
            or actor.email
            or ""
        )
        return bool(
            request_notification(
                "workspace.member_password_reset",
                variables={
                    "workspace_name": workspace.name,
                    "actor_name": actor_name,
                    "login_url": _frontend_url("/login"),
                },
                source_service="workspaces",
                user_id=str(member.user_id),
            )
        )
    except Exception:
        logger.exception(
            "failed to request password-reset notification for member %s in %s",
            member.pk,
            workspace.pk,
        )
        return False


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
