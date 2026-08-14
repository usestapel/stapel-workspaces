"""Service-layer helpers for workspaces (creation, invites, provision, suspension)."""

import logging
import os
import uuid
from datetime import timedelta
from secrets import token_urlsafe

import requests
from django.conf import settings
from django.db import models, transaction
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

from .conf import (
    CREATE_POLICY_CLOSED,
    CREATE_POLICY_OPEN,
    email_initial_password,
    login_grant_ttl_seconds,
    resend_cooldown_seconds,
    rotate_token_on_resend,
    workspace_create_policy,
    workspaces_settings,
)
from .dto import WorkspaceSecuritySettings
from .entitlements import (
    ENT_MEMBERS_MAX,
    EntitlementDenied,
    check_org_entitlement,
    debit_provision_credits,
    member_seats_quantity,
    refund_provision_credits,
)
from .capabilities import capabilities_for
from .events import (
    EVENT_WORKSPACE_INVITATION_REVOKED,
    EVENT_WORKSPACE_MEMBER_PASSWORD_RESET,
    EVENT_WORKSPACE_MEMBER_PROVISIONED,
    EVENT_WORKSPACE_MEMBER_REMOVED,
    EVENT_WORKSPACE_MEMBER_ROLE_CHANGED,
    EVENT_WORKSPACE_MEMBER_SUSPENDED,
    EVENT_WORKSPACE_MEMBER_UNSUSPENDED,
    EVENT_WORKSPACE_PERSONAL_CREATED,
)
from .audit import record_audit  # noqa: F401 - the one write path; see audit.py
from .models import (
    SUSPENSION_ACCOUNT_DEACTIVATED,
    SUSPENSION_NO_MFA,
    AuditAction,
    InvitationStatus,
    MFAEnforcementState,
    ProvisionState,
    Role,
    Workspace,
    WorkspaceInvitation,
    WorkspaceMember,
    WorkspaceMFAEnforcement,
    WorkspaceProvisionOperation,
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
# stapel-profiles integration — by NAME over comm, never by symbol
# ---------------------------------------------------------------------------
#
# This module's own convention (MODULE.md: "Stapel modules never import each
# other; all cross-module communication goes through stapel-core") rules out
# ``from stapel_profiles... import ...`` here — same as every other sibling
# seam in this file (auth, notifications, billing), all of which go through
# stapel-core instead of a direct import.
#
# Until 0.21.0 that convention was honoured in the letter and broken in the
# spirit: a helper asked Django's app registry whether ``stapel_profiles``
# ran in THIS process and then resolved that module's internals by dotted
# path (``validate_display_name``, ``get_profile_model``,
# ``publish_profile_changed``). It was the fleet's only cross-module symbol
# resolution, and it made a product feature a function of topology — where
# profiles is its own container (ironmemo's actual deployment) the roster's
# name-edit endpoint answered a PERMANENT ``error.503.profiles_unavailable``
# whose remediation told the caller to wait for a module that was never
# coming.
#
# The verdict (`tasks/who-owns-the-name-write.md`): authority decides, the
# owner writes. The endpoint stays here — rank semantics ("only an owner
# renames an owner") live nowhere else — and the write goes through named
# comm operations stapel-profiles publishes, exactly as ``billing.debit``
# lets this module move credits in billing's ledger. Comm Functions are
# topology-independent by construction: in-process in a monolith, internal
# HTTP/NATS in a split deployment, chosen by STAPEL_COMM, not by code here.
#
# Deployment floor: stapel-profiles >= 0.10 (the release that publishes
# these three). ``checks.check_profiles_name_write_wired`` says so out loud
# at startup when neither a provider nor a route can be found.
SET_DISPLAY_NAME = "profiles.set_display_name"
VALIDATE_DISPLAY_NAME = "profiles.validate_display_name"
DISPLAY_NAMES = "profiles.display_names"

#: Refusal reasons ``profiles.set_display_name`` answers with that are name
#: verdicts: the wire form is the trailing name of stapel-profiles' own
#: ``error.400.display_name_*`` keys, which this module re-declares, so the
#: mapping back is a prefix and not a translation table that can drift.
_DISPLAY_NAME_REASON_PREFIX = "display_name_"


# PROFILES_SERVICE_URL follows the same flat-setting canon as FRONTEND_URL
# above and stapel-core's WORKSPACES_SERVICE_URL: unset (the default) turns
# the HTTP read fallback off, no network attempt is ever made, and every
# caller falls back to WorkspaceMember.display_name_hint alone.
def _profiles_service_url() -> str:
    return (
        getattr(settings, "PROFILES_SERVICE_URL", "")
        or os.environ.get("PROFILES_SERVICE_URL", "")
    ).rstrip("/")


def check_display_name(value: str) -> str | None:
    """stapel-profiles' name canon, asked by name. Error key, or ``None``.

    The single canon for what a display name may contain — minimum length,
    control/invisible characters, emoji. This module deliberately owns NO
    second copy of those rules: profiles' llms.txt names re-deriving them as
    the mistake, and a weaker duplicate inside the framework that canonizes
    the original is exactly the drift the fleet keeps paying for.

    Returns one of the four ``error.400.display_name_*`` keys this module
    re-declares, or ``None`` when the name is fine.

    **Best-effort, and deliberately so.** When profiles publishes no
    reachable provider the answer is ``None`` — the same "no canon here"
    this module answered before 0.21.0 when the sibling was not in the
    process. Nothing is substituted for it: a made-up local rule is the
    drift above. The two callers cover the gap differently, and both are
    honest about it — a member rename cannot silently skip the canon
    because ``set_profile_display_name`` re-runs it INSIDE profiles and
    fails loudly if that call cannot be made at all, while an invitation's
    local name hint keeps exactly the rule this module already applied to
    the same column at invite time (the storage ceiling).

    The length CEILING is not part of this: 35 characters is a storage fact
    that ``Profile.display_name`` and ``WorkspaceInvitation.display_name_hint``
    both declare, enforced as the serializer field's ``max_length``
    (``error.400.field.max_length``).
    """
    try:
        result = call(VALIDATE_DISPLAY_NAME, {"display_name": value}) or {}
    except (FunctionNotRegistered, FunctionRouteNotConfigured) as exc:
        logger.debug(
            "%s has no provider in this deployment (%s) — the name canon is "
            "not applied here",
            VALIDATE_DISPLAY_NAME,
            exc,
        )
        return None
    except FunctionCallError:
        logger.warning("%s failed", VALIDATE_DISPLAY_NAME, exc_info=True)
        return None
    if result.get("ok"):
        return None
    return _error_key_for_reason(result.get("reason"))


def _error_key_for_reason(reason) -> str | None:
    """profiles' structural ``reason`` -> the error key to answer with.

    ``display_name_emoji`` -> ``error.400.display_name_emoji``: the same
    string key, the same English, the same remediation a frontend already
    branches on when stapel-profiles itself refused the write. Any other
    reason (today: ``no_display_name_field`` — this deployment's profile
    model carries no name at all, §66) is not something the caller can edit,
    so it collapses onto the 503 the absent-module case answers with.
    """
    if isinstance(reason, str) and reason.startswith(_DISPLAY_NAME_REASON_PREFIX):
        return f"error.400.{reason}"
    return None


def set_profile_display_name(user_id, display_name: str) -> str | None:
    """Write *display_name* as the canonical name of *user_id*.

    Returns ``None`` on success, or the error key the caller must answer
    with — so the endpoint's refusal vocabulary is decided here, in one
    place, from one structural result.

    The canonical name lives in stapel-profiles and nowhere else — writing
    ``WorkspaceMember.display_name_hint`` instead would be writing a field
    that goes dark the moment a profile exists (see its docstring in
    ``models.py``), i.e. a rename the person renamed never sees. So this is
    a call to ``profiles.set_display_name``, the named write that module
    publishes: it validates against its own canon, resolves the possibly
    host-swapped profile model, creates the row when the person has never
    opened the profile screen, and publishes ``profile.changed`` so every
    downstream consumer of that name follows. None of those four things is
    reimplemented here, and after 0.21.0 none of them CAN be — there is no
    seam left to reach them through.

    The three failure keys, and why they are three:

    * ``error.400.display_name_*`` — profiles refused the name. Verbatim
      passthrough of its own keys; the frontend that highlights the field on
      a refusal from ``PATCH /profiles/me`` behaves identically here.
    * ``error.503.profiles_not_configured`` — no provider and no route.
      A CONFIGURATION fact (env-address-class v2 §2): deterministic, fixed
      by editing this deployment's STAPEL_COMM, and it will NOT heal on its
      own. Telling the caller to wait would be the lie 0.19.0 told.
    * ``error.503.profiles_unavailable`` — the call was made and failed, or
      profiles answered a refusal that is not about the name. Genuinely
      transient / genuinely someone else's outage: ``wait_and_retry`` is
      true here and only here.
    """
    from .errors import ERR_503_PROFILES_NOT_CONFIGURED, ERR_503_PROFILES_UNAVAILABLE

    try:
        result = call(
            SET_DISPLAY_NAME,
            {"user_id": str(user_id), "display_name": display_name},
        ) or {}
    except (FunctionNotRegistered, FunctionRouteNotConfigured) as exc:
        # Loud, and at ERROR: this is a deployment that ships the roster's
        # name-edit endpoint with nothing behind it, and every request to it
        # will fail the same way until somebody edits configuration.
        logger.error(
            "%s is not wired in this deployment (%s) — the roster cannot "
            "write a canonical name here. Either co-mount stapel-profiles "
            ">= 0.10 in this process or add a STAPEL_COMM FUNCTION_ROUTES "
            "entry for 'profiles.' pointing at the service that runs it.",
            SET_DISPLAY_NAME,
            exc,
        )
        return ERR_503_PROFILES_NOT_CONFIGURED
    except FunctionCallError:
        logger.warning("%s failed", SET_DISPLAY_NAME, exc_info=True)
        return ERR_503_PROFILES_UNAVAILABLE

    if result.get("ok"):
        return None
    # A name verdict passes through keyed; anything else (a deployment whose
    # profile model has no display_name at all) is not the caller's to fix
    # and is not a transient outage either — but it IS "profiles cannot
    # serve this", which is what the pre-existing 503 key already says.
    return _error_key_for_reason(result.get("reason")) or ERR_503_PROFILES_UNAVAILABLE


def _fetch_profile_display_names(user_ids) -> dict:
    """Best-effort ``{str(user_id): display_name}`` from stapel-profiles.

    Only ids with a NON-EMPTY name there are present in the result — an id
    with no profile row, or an empty ``display_name``, is simply absent,
    never a placeholder (mirrors ``ProfileBatchResponse``'s own "missing is
    not invented" contract). Callers fall back further to
    ``WorkspaceMember.display_name_hint`` for anything absent here.

    Best-effort like every other optional cross-service seam this module
    already has (auth/notifications/billing): no provider, no route, a
    transport error, a routing miss (this service and stapel-profiles
    disagree about the path — see ``service_answered``), or any non-200 all
    degrade to ``{}`` rather than raising. A member's name is cosmetic; it
    is never worth failing a roster over.
    """
    ids = list(dict.fromkeys(str(uid) for uid in user_ids))
    if not ids:
        return {}

    # comm FIRST, and it covers both topologies: in a monolith the transport
    # is in-process, so the sibling sitting right there in INSTALLED_APPS is
    # found without any service URL (nobody points a service at itself —
    # measured live on meettoday 2026-08-05: profiles installed in the same
    # process, name never found, invitation emails addressed from a bare
    # email address); in a split deployment the same call goes over the
    # configured route. profiles.display_names is swap-aware on its side, so
    # a host that put its names on its own extended Profile is honoured
    # (SWAP001) without this module knowing that model exists.
    try:
        result = call(DISPLAY_NAMES, {"user_ids": ids}) or {}
        names = {
            str(uid): name
            for uid, name in (result.get("display_names") or {}).items()
            if (name or "").strip()
        }
        if names:
            return names
    except (FunctionNotRegistered, FunctionRouteNotConfigured) as exc:
        # Not an error: a deployment may serve names over the HTTP batch
        # below (stapel-profiles < 0.10, which published no functions), or
        # may have no profiles at all.
        logger.debug("%s unavailable (%s)", DISPLAY_NAMES, exc)
    except FunctionCallError:
        logger.warning("%s failed", DISPLAY_NAMES, exc_info=True)

    # HTTP fallback: stapel-profiles' public, AllowAny
    # ``POST /profiles/api/v1/batch`` — built by that module for exactly this
    # "resolve many ids at once" shape — using stapel-core's own peer-client
    # discipline (``service_answered``: a routing 404 is never a verdict)
    # rather than reading a network hiccup as "nobody has a name". Kept for
    # deployments wired that way before profiles published a read function.
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


class LastOwnerError(Exception):
    """The write would leave the workspace with no owner.

    Raised by :func:`change_member_role` / :func:`remove_member` when the
    invariant is re-checked under the workspace lock and no other owner is
    left. The view renders it as ``error.403.last_owner_cannot_be_removed``.
    """


def lock_workspace(workspace) -> Workspace | None:
    """Take the workspace's row lock — the mutex of every seat/owner write.

    Accepts a :class:`~stapel_workspaces.models.Workspace` or its id and
    returns the freshly locked row (``None`` when it is gone or
    soft-deleted). Callers must already be inside ``transaction.atomic``.

    Every path that changes WHO is in a workspace — invite, resend, accept,
    provision, role change, removal — takes this lock BEFORE reading the
    counts it decides on, and always in this order (workspace row first,
    then the invitation/member row), so the paths cannot deadlock against
    each other. Deciding on a count read outside the lock is deciding on a
    snapshot another transaction is already invalidating: two last-owner
    demotions each saw a second owner, two invite batches each saw the last
    free seat, and both committed.

    SQLite ignores ``SELECT ... FOR UPDATE`` (Django's backend has no
    support flag for it), so the serialization this buys is real only on a
    locking database. The decisions themselves are re-made inside the lock
    either way, which is what the tests pin.
    """
    workspace_id = getattr(workspace, "pk", workspace)
    return (
        Workspace.objects.select_for_update()
        .filter(pk=workspace_id, deleted_at__isnull=True)
        .first()
    )


@transaction.atomic
def invite_members(
    *,
    workspace: Workspace,
    emails,
    role: str,
    invited_by,
    display_name: str | None = None,
) -> list[WorkspaceInvitation]:
    """Reserve seats and create the batch's invitations in one transaction.

    The seat ceiling is counted under :func:`lock_workspace` and the rows
    are written before the lock is released, so a seat cannot be sold
    twice: a second batch arriving at the same moment waits, then counts
    the rows this one has already committed. The view's own check is a
    hint that produces a readable 402 early; this one is the reservation.

    Raises :class:`~stapel_workspaces.entitlements.EntitlementDenied` when
    the plan cannot carry the batch, and ``Workspace.DoesNotExist`` when
    the workspace disappeared between the view's read and the lock.
    """
    locked = lock_workspace(workspace)
    if locked is None:
        raise Workspace.DoesNotExist("workspace is gone")
    emails = list(emails)
    verdict = check_org_entitlement(
        locked,
        ENT_MEMBERS_MAX,
        quantity=member_seats_quantity(locked, additional=len(emails)),
    )
    if not verdict.allowed:
        raise EntitlementDenied(ENT_MEMBERS_MAX, verdict)
    return [
        create_invitation(
            workspace=locked,
            email=email,
            role=role,
            invited_by=invited_by,
            display_name=display_name,
        )
        for email in emails
    ]


@transaction.atomic
def create_invitation(
    *,
    workspace: Workspace,
    email: str,
    role: str,
    invited_by,
    display_name: str | None = None,
) -> WorkspaceInvitation:
    """Create — or refresh — the workspace's live invitation for *email*.

    One live invitation per address per workspace, and the database says
    so (``workspaces_invitation_one_live_per_email``). Inviting an address
    that already has an unresolved invitation used to insert a second row,
    and each row reserved its own seat: an admin re-sending from the
    invite modal instead of the resend button billed the org twice for one
    person and handed out two working tokens for one seat. The re-invite
    now lands on the existing row — role, name hint and TTL refreshed, the
    letter sent again — so the address has exactly one way in.
    """
    email = email.lower().strip()
    expires_at = timezone.now() + timedelta(
        days=workspaces_settings.INVITATION_TTL_DAYS
    )
    invitation = (
        WorkspaceInvitation.objects.select_for_update()
        .unresolved()
        .filter(workspace=workspace, email=email)
        .first()
    )
    if invitation is not None:
        invitation.role = role
        invitation.expires_at = expires_at
        invitation.display_name_hint = (display_name or "").strip()
        invitation.save(update_fields=["role", "expires_at", "display_name_hint"])
    else:
        invitation = WorkspaceInvitation.objects.create(
            workspace=workspace,
            email=email,
            role=role,
            invited_by=invited_by,
            token=token_urlsafe(32),
            expires_at=expires_at,
            # A NAME HINT (this invite's "Name" field), not the canonical
            # name — see WorkspaceMember.display_name_hint's docstring for
            # why this module stores it at all despite the name living in
            # stapel-profiles.
            display_name_hint=(display_name or "").strip(),
        )
    record_audit(
        workspace=workspace,
        action=AuditAction.INVITATION_CREATED,
        actor=invited_by,
        subject_email=invitation.email,
        role=role,
    )
    _send_invitation_notification(invitation)
    return invitation


def _frontend_url(path: str) -> str:
    """Absolute frontend URL for *path* (FRONTEND_URL flat-setting canon)."""
    frontend_url = (getattr(settings, "FRONTEND_URL", "") or "").rstrip("/")
    return f"{frontend_url}{path}" if frontend_url else path


#: Notification type of the FIRST invitation letter to an address that
#: already has an account here.
NOTIFICATION_INVITATION = "workspace.invitation"
#: Notification type of the first invitation letter to an address with NO
#: account yet: the same link both creates the account and joins the
#: workspace, and that letter has to say so. Its own type in the
#: notifications catalog (>= 0.6.1) with its own template and copy.
NOTIFICATION_INVITATION_NEW_USER = "workspace.invitation.new_user"
#: Notification type of a re-delivery (admin "resend").
NOTIFICATION_INVITATION_REMINDER = "workspace.invitation.reminder"


def _send_invitation_notification(
    invitation: WorkspaceInvitation,
    *,
    notification_type: str | None = None,
) -> None:
    """Ask stapel-notifications to deliver the invite email.

    Best-effort: a delivery hiccup must never break invitation creation —
    the invite stays listable/resendable either way.

    ``notification_type`` distinguishes the first letter from a
    re-delivery: the resend path passes
    :data:`NOTIFICATION_INVITATION_REMINDER` (its own type in the
    notifications catalog, >= 0.6.1), because "you are being reminded" is a
    different message from "you are being invited". Same variables either
    way.

    Left unset (the create path), the type is CHOSEN here, between
    :data:`NOTIFICATION_INVITATION` and
    :data:`NOTIFICATION_INVITATION_NEW_USER`, on whether the invited address
    already has an account. That branch is the whole reason the second type
    exists: its letter says "the button below creates your account and
    joins you", which is a lie to somebody who already has one and the only
    honest sentence for somebody who does not. The type, its template, its
    translation keys and its routing entry all shipped in stapel-
    notifications 0.6.1 — and for two minor versions nothing ever selected
    it, so every invitee got the has-an-account copy. ``tests/
    test_invitation_letter.py`` now fails if any ``workspace.*`` type in the
    catalog is unreachable from this module again.

    The branch deliberately changes NOTHING an outsider can observe. It is
    the same ``invitee`` row already fetched for ``user_id`` below, and:
    the create endpoint has no duplicate/exists branch, its 201 body is the
    same DTO either way, no event is emitted, and neither branch logs. The
    only difference is which of two letters lands in the invited address's
    OWN mailbox — the holder of that address learning a fact about their
    own address. Account existence stays answerable from outside only where
    this module deliberately answers it: ``email_registered`` on the
    token-gated, email-masked, throttled invitation preview.
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
        # The branch the second template was built for, off the row that was
        # fetched one line up anyway (see this function's docstring for why
        # it is not an account-existence oracle).
        if notification_type is None:
            notification_type = (
                NOTIFICATION_INVITATION
                if invitee is not None
                else NOTIFICATION_INVITATION_NEW_USER
            )
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
        # The letter is with the mailer: start the cooldown clock. Written
        # here, on the ONE path that requests an invitation letter, so
        # "when was this address last mailed about this invitation" cannot
        # drift from "when did we last mail it" the way a caller-side stamp
        # would. A request that never got this far (no notifications module,
        # a transport error — the except below) leaves the clock alone: no
        # letter was sent, so nothing is owed a cooldown.
        stamped = timezone.now()
        WorkspaceInvitation.objects.filter(pk=invitation.pk).update(
            last_sent_at=stamped
        )
        invitation.last_sent_at = stamped
    except Exception:
        logger.exception(
            "failed to request invitation notification for %s", invitation.pk
        )


#: comm Function owned by stapel-auth (>= 0.11): mints a single-use login
#: grant token bound to an email (org-program spec §B3).
ISSUE_LOGIN_GRANT = "auth.issue_login_grant"


class LoginGrantAlreadyIssued(Exception):
    """A live login grant already exists for this invitation (WORK-03).

    Carries ``retry_after`` seconds — the remainder of the grant's TTL,
    after which a genuine "the email never arrived" retry is allowed again.
    """

    def __init__(self, retry_after: int):
        self.retry_after = retry_after
        super().__init__(f"a login grant is live for {retry_after}s")


def _claim_login_grant_window(invitation: WorkspaceInvitation) -> int:
    """Atomically take the invitation's single live-grant slot.

    A conditional UPDATE, not a read-then-write: the row is claimed by the
    statement that finds it unclaimed (or expired), so two simultaneous
    claims of one invite token mint one grant and the loser is told when to
    come back. Returns 0 when the slot was taken here, else the seconds
    remaining.
    """
    ttl = login_grant_ttl_seconds()
    if ttl <= 0:
        return 0
    now = timezone.now()
    cutoff = now - timedelta(seconds=ttl)
    claimed = (
        WorkspaceInvitation.objects.filter(pk=invitation.pk)
        .filter(
            models.Q(login_grant_issued_at__isnull=True)
            | models.Q(login_grant_issued_at__lt=cutoff)
        )
        .update(
            login_grant_issued_at=now,
            login_grant_count=models.F("login_grant_count") + 1,
        )
    )
    if claimed:
        return 0
    issued_at = (
        WorkspaceInvitation.objects.filter(pk=invitation.pk)
        .values_list("login_grant_issued_at", flat=True)
        .first()
    )
    if issued_at is None:
        return 0
    remaining = ttl - (now - issued_at).total_seconds()
    return max(1, int(remaining + 0.999)) if remaining > 0 else 0


@transaction.atomic
def issue_invitation_login_grant(
    *, invitation: WorkspaceInvitation, language: str | None = None
) -> str:
    """Mint a login grant for a not-yet-registered invitee (claim step).

    Calls ``auth.issue_login_grant`` with ``create_if_missing`` — the
    verified account materializes when the holder exchanges the grant at
    auth's ``/grant/exchange/``. The invitation is deliberately NOT
    consumed: accept stays a separate, conscious step after account setup.

    ONE LIVE GRANT AT A TIME (WORK-03). The grant is single-use in auth,
    but nothing here stopped the same invite token from minting another
    one, and another: a link that leaked out of a mailbox was an unbounded
    supply of session-bearing credentials for that address. The slot is
    claimed by a conditional UPDATE inside this transaction — so two
    simultaneous claims produce one grant, and an auth failure rolls the
    claim back rather than burning the window — and it reopens after
    ``STAPEL_WORKSPACES["INVITATION_LOGIN_GRANT_TTL_SECONDS"]``, which is
    what makes "the email never arrived" still workable. Raises
    :class:`LoginGrantAlreadyIssued` while a grant is live.

    What cannot be fixed from this side: auth's ``ISSUE_LOGIN_GRANT_SCHEMA``
    takes only the email (``additionalProperties: false``), so the grant
    cannot be bound to this workspace, this invitation or a purpose, and
    its TTL is auth's own. Binding it needs a stapel-auth change; the
    window above is the containment this repository can give.

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
    remaining = _claim_login_grant_window(invitation)
    if remaining:
        raise LoginGrantAlreadyIssued(remaining)
    payload: dict = {
        "email": invitation.email,
        "verified_email": True,
        "create_if_missing": True,
    }
    if language:
        payload["language"] = language
    result = call(ISSUE_LOGIN_GRANT, payload) or {}
    # "A NEW PERSON APPEARED IN THE WORLD", recorded separately from "a known
    # person joined us" (INVITATION_ACCEPTED, which an existing account also
    # performs). The owner asked for both, and they genuinely differ: the claim
    # path is the only one where the account itself did not exist before.
    # `created` is auth's own answer — it knows whether it minted one; absent
    # (an older auth), no line rather than a guessed one.
    if result.get("created"):
        record_audit(
            workspace=invitation.workspace_id,
            action=AuditAction.ACCOUNT_CREATED_BY_INVITATION,
            subject_email=invitation.email,
            role=invitation.role,
        )
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
    # WHO, not only when. Same provenance shape as `invited_by` on the
    # opposite transition; see WorkspaceInvitation.revoked_by. The emit
    # below carried the actor from the start, but a bus message is not a
    # record this service can be asked a question about afterwards, so
    # "who withdrew that invite" had no answer at all in the API.
    locked.revoked_by = revoked_by
    locked.save(update_fields=["revoked_at", "revoked_by"])
    record_audit(
        workspace=locked.workspace_id,
        action=AuditAction.INVITATION_REVOKED,
        actor=revoked_by,
        subject_email=locked.email,
        role=locked.role,
    )
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


class InvitationResendCooldown(Exception):
    """A resend was refused because the address was mailed too recently.

    Carries ``retry_after`` — whole seconds until the next letter is
    allowed, always at least 1 — so the caller can answer with a number the
    admin can act on instead of a bare "no".
    """

    def __init__(self, retry_after: int):
        self.retry_after = retry_after
        super().__init__(f"resend cooldown: {retry_after}s remaining")


def resend_cooldown_remaining(invitation: WorkspaceInvitation) -> int:
    """Seconds before this invitation's address may be mailed again (0 = now).

    The clock is the most recent ``last_sent_at`` of ANY invitation to the
    same address in the same workspace, not just this row's. Both readings
    stop the same loop, but the address-wide one is the honest unit: the
    thing a resend loop damages is one person's inbox, and re-inviting the
    same address produces another row that would otherwise start with a
    fresh, empty clock.

    Returns 0 when the cooldown is disabled
    (``INVITATION_RESEND_COOLDOWN_SECONDS`` of 0 or None), when nothing was
    ever sent (rows predating the column; a deployment with no notifications
    service, where no letter exists to be repeated), or when the window has
    passed.
    """
    cooldown = resend_cooldown_seconds()
    if cooldown <= 0:
        return 0
    last_sent = (
        WorkspaceInvitation.objects.filter(
            workspace_id=invitation.workspace_id,
            email__iexact=invitation.email,
            last_sent_at__isnull=False,
        )
        .order_by("-last_sent_at")
        .values_list("last_sent_at", flat=True)
        .first()
    )
    if last_sent is None:
        return 0
    elapsed = (timezone.now() - last_sent).total_seconds()
    remaining = cooldown - elapsed
    return max(1, int(remaining + 0.999)) if remaining > 0 else 0


def resend_invitation(*, invitation: WorkspaceInvitation) -> WorkspaceInvitation:
    """Re-deliver an invitation: extend the TTL, mail it again (#109).

    The reason an admin resends is almost always that the first letter
    never arrived or the TTL ran out, so this deliberately accepts an
    EXPIRED invitation: the compare-and-set is the clock-free
    ``unresolved()``, which excludes exactly the three terminal timestamps
    and nothing else. An accepted, declined or revoked invitation is never
    resendable — those are decisions, not delivery failures.

    **Cooldown.** Raises :class:`InvitationResendCooldown` when the invited
    address was mailed less than
    ``STAPEL_WORKSPACES["INVITATION_RESEND_COOLDOWN_SECONDS"]`` ago (10
    minutes by default; see that key for why it is a per-address duration
    and not a per-caller DRF rate). The check is inside the row lock —
    two admins pressing "resend" at the same instant are the case a
    read-then-write check outside it would let through, and the whole point
    is that only ONE letter leaves.

    **The token is reused, not rotated**, unless the deployment sets
    ``INVITATION_ROTATE_TOKEN_ON_RESEND``. The reasoning is written out at
    that key in :mod:`stapel_workspaces.conf`; the short form is that the
    resend goes to the same mailbox as the original, so rotation buys no
    containment and costs the invitee a dead link in the letter they are
    most likely to click. Reversed from the pre-0.23 hardcoded rotation.

    ``expires_at`` restarts from now (``INVITATION_TTL_DAYS``) — a resent
    invitation the invitee cannot use before it expires again is not a
    resend. Reviving an EXPIRED invitation re-takes a seat, so the plan
    ceiling is re-counted here under :func:`lock_workspace` and
    :class:`~stapel_workspaces.entitlements.EntitlementDenied` is raised
    when it no longer fits. The view's own check answers the admin early;
    this one is what a batch of simultaneous resends has to pass.
    """
    with transaction.atomic():
        # Workspace row first, invitation row second — the lock order every
        # seat/owner path in this module takes, so they queue instead of
        # deadlocking.
        workspace = lock_workspace(invitation.workspace_id)
        if workspace is None:
            raise ValueError("workspace is gone")
        locked = (
            WorkspaceInvitation.objects.select_for_update()
            .unresolved()
            .filter(pk=invitation.pk)
            .first()
        )
        if locked is None:
            raise ValueError("invitation is not pending")
        # A live invitation already holds its seat; a revived expired one
        # has to be given one back, and only if the plan still has it.
        if locked.status != InvitationStatus.PENDING:
            verdict = check_org_entitlement(
                workspace,
                ENT_MEMBERS_MAX,
                quantity=member_seats_quantity(workspace, additional=1),
            )
            if not verdict.allowed:
                raise EntitlementDenied(ENT_MEMBERS_MAX, verdict)
        remaining = resend_cooldown_remaining(locked)
        if remaining:
            raise InvitationResendCooldown(remaining)
        updated = ["expires_at", "last_sent_at"]
        if rotate_token_on_resend():
            locked.token = token_urlsafe(32)
            updated.append("token")
        locked.expires_at = timezone.now() + timedelta(
            days=workspaces_settings.INVITATION_TTL_DAYS
        )
        # Claim the window HERE, under the lock, not when the letter comes
        # back from the mailer: the send happens after this transaction
        # commits, and two admins pressing "resend" together would otherwise
        # both pass a check that reads a stamp neither has written yet. A
        # letter that never reaches the mailer still spends the window —
        # fail-closed is the only safe direction for a rate limit.
        locked.last_sent_at = timezone.now()
        locked.save(update_fields=updated)
    # Outside the row lock: delivery is best-effort and must not hold a
    # write lock open across a cross-service notification call. A resend is
    # a reminder, not a first invitation — its own notification type, so
    # the letter can say "you are being reminded" honestly.
    _send_invitation_notification(
        locked, notification_type=NOTIFICATION_INVITATION_REMINDER
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
    record_audit(
        workspace=locked.workspace_id,
        action=AuditAction.INVITATION_DECLINED,
        actor=user,
        subject=user,
        subject_email=locked.email,
        role=locked.role,
    )
    return locked


@transaction.atomic
def accept_invitation(*, invitation: WorkspaceInvitation, user) -> WorkspaceMember:
    # Lock the invitation row: a single-use token must not be consumable
    # twice by concurrent requests. The compare-and-set is the same
    # unresolved() as decline's — hand-written, it had lost the revoked_at
    # clause, so a revocation committing between the view's state check and
    # this lock lost the race and the invite was accepted anyway (0.10.0).
    # Workspace row first, invitation row second (see lock_workspace): the
    # seat this accept turns into a membership is counted under that lock,
    # so a batch of invitees accepting at the same instant cannot each read
    # the same last free seat and all take it.
    workspace = lock_workspace(invitation.workspace_id)
    if workspace is None:
        raise ValueError("workspace is gone")
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
            workspace,
            ENT_MEMBERS_MAX,
            quantity=member_seats_quantity(workspace),
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
    # TWO lines, not one: "this invitation was taken up" and "this person is
    # now in the organization" are separate facts the owner asked to track
    # separately, and they can come apart — a re-accept of an existing
    # membership is an acceptance that joins nobody.
    record_audit(
        workspace=locked.workspace,
        action=AuditAction.INVITATION_ACCEPTED,
        # The recipient is the actor here: nobody else can accept for them.
        actor=user,
        subject=user,
        subject_email=locked.email,
        role=locked.role,
    )
    if not already_member:
        record_audit(
            workspace=locked.workspace,
            action=AuditAction.MEMBER_JOINED,
            actor=user,
            subject=user,
            subject_email=locked.email,
            role=member.role,
            invited_by=str(locked.invited_by_id) if locked.invited_by_id else None,
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


@transaction.atomic
def change_member_role(*, member: WorkspaceMember, new_role: str, actor):
    """Write a member's new role with the last-owner invariant held.

    "Is there another owner" is answered INSIDE the workspace lock and
    immediately before the write, because the answer is only true until
    somebody else commits. Two admins demoting the two remaining owners at
    the same moment each read "yes, one other owner exists" outside a lock,
    and the workspace ended up with none — an organization nobody can
    administer, recoverable only by hand in the database.

    Raises :class:`LastOwnerError` when this write would take the last
    owner away, and ``WorkspaceMember.DoesNotExist`` when the row (or the
    workspace) went away first. Returns the saved member.
    """
    if lock_workspace(member.workspace_id) is None:
        raise WorkspaceMember.DoesNotExist("workspace is gone")
    locked = (
        WorkspaceMember.objects.select_for_update()
        .select_related("workspace", "user")
        .filter(pk=member.pk)
        .first()
    )
    if locked is None:
        raise WorkspaceMember.DoesNotExist("member is gone")
    if locked.role == Role.OWNER and new_role != Role.OWNER:
        _assert_another_owner(locked)
    old_role = locked.role
    locked.role = new_role
    locked.save(update_fields=["role"])
    # Transactional outbox: leaves iff this transaction commits.
    # Cross-service consumers (e.g. a rooms service re-evaluating a
    # participant's rights) get the new role's capability grants inline
    # (spec §A4).
    emit(
        EVENT_WORKSPACE_MEMBER_ROLE_CHANGED,
        {
            "workspace_id": str(locked.workspace_id),
            "user_id": str(locked.user_id),
            "old_role": str(old_role),
            "new_role": str(locked.role),
            "capabilities": capabilities_for(locked.role),
        },
    )
    record_audit(
        workspace=locked.workspace_id,
        action=AuditAction.MEMBER_ROLE_CHANGED,
        actor=actor,
        subject=locked.user_id,
        role=locked.role,
        old_role=str(old_role),
        new_role=str(locked.role),
    )
    return locked


@transaction.atomic
def remove_member(*, member: WorkspaceMember, actor):
    """Delete a membership with the last-owner invariant held.

    The serialized twin of :func:`change_member_role` — same lock, same
    re-read, same reason: a removal and a demotion racing each other are
    two writes that were each valid against a workspace that no longer
    existed by the time they landed.

    Raises :class:`LastOwnerError` / ``WorkspaceMember.DoesNotExist``.
    Returns ``(workspace, user, role)`` of the membership that was removed.
    """
    if lock_workspace(member.workspace_id) is None:
        raise WorkspaceMember.DoesNotExist("workspace is gone")
    locked = (
        WorkspaceMember.objects.select_for_update()
        .select_related("workspace", "user")
        .filter(pk=member.pk)
        .first()
    )
    if locked is None:
        raise WorkspaceMember.DoesNotExist("member is gone")
    if locked.role == Role.OWNER:
        _assert_another_owner(locked)
    workspace = locked.workspace
    removed_user = locked.user
    removed_role = locked.role
    locked.delete()
    # Transactional outbox: leaves iff this transaction commits. The
    # cross-service kick signal (spec §A4) — e.g. a rooms service
    # disconnects the user from an ongoing call.
    emit(
        EVENT_WORKSPACE_MEMBER_REMOVED,
        {
            "workspace_id": str(workspace.id),
            "user_id": str(removed_user.pk),
            "role": str(removed_role),
            "removed_by": str(getattr(actor, "pk", actor)),
        },
    )
    # The row is gone; the record of its going is not. An audit written
    # outside this transaction could survive a rollback and claim a removal
    # that never happened.
    record_audit(
        workspace=workspace,
        action=AuditAction.MEMBER_REMOVED,
        actor=actor,
        subject=removed_user,
        subject_email=getattr(removed_user, "email", "") or "",
        role=removed_role,
    )
    return workspace, removed_user, removed_role


def _assert_another_owner(member: WorkspaceMember) -> None:
    """Raise :class:`LastOwnerError` unless a second owner survives *member*.

    Counted under the caller's workspace lock, never before it.
    """
    others = (
        WorkspaceMember.objects.select_for_update()
        .filter(workspace_id=member.workspace_id, role=Role.OWNER)
        .exclude(pk=member.pk)
        .exists()
    )
    if not others:
        raise LastOwnerError("the last owner cannot be demoted or removed")


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


#: Namespace for the derived provisioning operation id — retrying the same
#: username in the same workspace IS the same operation, so a client that
#: never learned about idempotency keys still gets idempotency.
PROVISION_NAMESPACE = uuid.UUID("6f9b5c1e-2f43-4a0c-9a5f-4a2f5f36f6f1")


def _open_provision_operation(*, workspace, username, credits, operation_id=None):
    """Find or start the saga row for this provisioning attempt."""
    key = str(
        operation_id
        or uuid.uuid5(PROVISION_NAMESPACE, f"{workspace.pk}:{username}")
    )
    operation, _ = WorkspaceProvisionOperation.objects.get_or_create(
        workspace=workspace,
        operation_id=key,
        defaults={"username": username, "credits": credits},
    )
    return operation


#: States from which a further call is a NEW attempt rather than a resume.
_PROVISION_SETTLED = (
    ProvisionState.COMPENSATING,
    ProvisionState.COMPENSATED,
    ProvisionState.FAILED,
)


def _advance_provision(operation, state, *, user_id=None, credits_to_refund=None):
    """Move the saga forward and say so in the same write."""
    operation.state = state
    fields = ["state"]
    if user_id is not None:
        operation.user_id = user_id
        fields.append("user_id")
    if credits_to_refund is not None:
        operation.credits_to_refund = credits_to_refund
        fields.append("credits_to_refund")
    operation.save(update_fields=fields)


def _compensate_provision(operation, *, reason: str) -> None:
    """Undo what this operation paid for, or record that it could not be.

    Never raises: it runs on the failure path, and a compensation that
    explodes would replace one lost charge with a lost error message. What
    it cannot refund it leaves as ``compensating`` with the amount on the
    row — the state ``reconcile_provision_operations`` looks for.
    """
    operation.last_error = reason[:2000]
    if not operation.credits_to_refund:
        operation.state = ProvisionState.FAILED
        operation.save(update_fields=["state", "last_error"])
        return
    refunded = refund_provision_credits(
        operation.workspace,
        operation_id=operation.operation_id,
        credits=operation.credits_to_refund,
        reason=reason,
    )
    if refunded:
        operation.credits_to_refund = 0
        operation.state = ProvisionState.COMPENSATED
    else:
        operation.state = ProvisionState.COMPENSATING
    operation.save(update_fields=["state", "last_error", "credits_to_refund"])


def reconcile_provision_operations(*, limit: int = 100) -> list:
    """Retry the refunds that could not be made when they were owed.

    The scheduled half of the saga's compensation
    (``manage.py reconcile_provisioning``). Idempotent: the refund carries
    a per-operation idempotency key, and a row whose debt is settled leaves
    the queue.

    Returns the operations it settled.
    """
    owed = WorkspaceProvisionOperation.objects.filter(
        state=ProvisionState.COMPENSATING
    ).select_related("workspace")[:limit]
    settled = []
    for operation in owed:
        if refund_provision_credits(
            operation.workspace,
            operation_id=operation.operation_id,
            credits=operation.credits_to_refund,
            reason=operation.last_error or "reconciliation",
        ):
            operation.credits_to_refund = 0
            operation.state = ProvisionState.COMPENSATED
            operation.save(update_fields=["credits_to_refund", "state"])
            settled.append(operation)
    return settled


def provision_member(
    *,
    workspace: Workspace,
    username_local: str,
    role: str,
    provisioned_by,
    password: str | None = None,
    display_name: str | None = None,
    email: str | None = None,
    operation_id: str | None = None,
):
    """Create an org-provisioned (synthetic) member (org-program spec §C1).

    A SAGA with a stable operation id, not a straight line (WORK-03).
    Provisioning spends money in billing, mints an account in auth and
    writes a membership here; there is no transaction across the three, so
    every step records where it got to on a
    :class:`~stapel_workspaces.models.WorkspaceProvisionOperation` row:

    ``started`` → debit (``charged``) → ``auth.provision_user``
    (``account_created``) → membership + emit (``completed``).

    * **Replay is free.** The operation id defaults to
      ``uuid5(workspace, username)``, so pressing the button again after a
      timeout is the SAME operation: a completed one answers with the
      member it already made (and no password — that was handed over
      once), and an interrupted one resumes without a second charge, since
      the debit carries the same idempotency key.
    * **Failure compensates.** A failure after the charge tries the refund
      (``entitlements.refund_provision_credits``) and, when billing cannot
      take it, keeps the debt on the row as ``compensating`` for
      ``manage.py reconcile_provisioning``. The orphan charge the audit
      found is now a queue with a number in it.
    * **An orphan account is findable.** ``user_id`` is written as soon as
      auth mints the account, so a membership that never landed leaves a
      row naming exactly what to clean up. Deleting it needs an auth-side
      seam this module does not have; the record is the half that is ours.

    Credentials delivery (the email nuance, spec §C1): a synthetic account
    normally has NO email — the ``workspace.provisioned_account`` letter is
    skipped and the server-generated password is returned to the
    provisioning admin in the API response, exactly once
    (``generated_password``). When the optional ``email`` IS passed the
    letter goes there — with the username, workspace and login URL, and
    WITHOUT the password unless the deployment sets
    ``STAPEL_WORKSPACES["PROVISION_EMAIL_INITIAL_PASSWORD"]``: a credential
    mailed in cleartext outlives its one use by the life of the mailbox.
    It never rides any event payload and is never logged.

    Returns ``(member, username, generated_password | None)``.
    Raises :class:`ProvisionError` on a structured auth failure and lets
    comm wiring errors (auth not installed/routed) propagate — the view
    maps them to 503, this seam never degrades to allow.
    """
    from django.contrib.auth import get_user_model

    username = f"{workspace.slug}/{username_local}"
    credits = int(workspaces_settings.PROVISION_USER_CREDITS or 0)
    operation = _open_provision_operation(
        workspace=workspace,
        username=username,
        credits=credits,
        operation_id=operation_id,
    )
    if operation.state == ProvisionState.COMPLETED:
        # A replay of a finished operation. The password is deliberately
        # NOT re-issued: it was delivered once, and minting a second answer
        # here would mean either storing it or resetting the account.
        member = WorkspaceMember.objects.filter(
            workspace=workspace, user_id=operation.user_id
        ).first()
        if member is not None:
            return member, operation.username, None
    if operation.state in _PROVISION_SETTLED:
        # A previous attempt failed and was compensated. This is a NEW
        # attempt of the same operation: same id (so the account and the
        # membership stay unique), fresh attempt number (so the charge for
        # it is a fresh charge and not a duplicate billing must dedupe).
        operation.attempt += 1
        operation.state = ProvisionState.STARTED
        operation.save(update_fields=["attempt", "state"])

    if credits > 0 and operation.state == ProvisionState.STARTED:
        debit_provision_credits(
            workspace,
            provision_id=f"{operation.operation_id}:{operation.attempt}",
            username=username,
            credits=credits,
        )
        _advance_provision(
            operation,
            ProvisionState.CHARGED,
            credits_to_refund=operation.credits_to_refund + credits,
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
    if operation.user_id is None:
        try:
            result = call(PROVISION_USER, payload) or {}
        except Exception as exc:
            _compensate_provision(operation, reason=f"{type(exc).__name__}: {exc}")
            raise
        if result.get("error"):
            _compensate_provision(operation, reason=result["error"])
            raise ProvisionError(result["error"])
        user_id = result["user_id"]
        generated_password = result.get("generated_password")
        _advance_provision(
            operation, ProvisionState.ACCOUNT_CREATED, user_id=user_id
        )
    else:
        # Resuming an operation whose account already exists: auth minted it
        # on an attempt that then failed, and asking again would collide
        # with its own username — the retry finishes the orphan instead of
        # tripping over it. No password: auth issued one, once.
        user_id = operation.user_id
        generated_password = None

    try:
        member = _complete_provision(
            operation=operation,
            workspace=workspace,
            user_id=user_id,
            role=role,
            provisioned_by=provisioned_by,
            display_name=display_name,
        )
    except Exception as exc:
        _compensate_provision(operation, reason=f"{type(exc).__name__}: {exc}")
        raise
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
            initial_password=(
                generated_password if email_initial_password() else None
            ),
        )
    return member, username, generated_password


def _complete_provision(
    *, operation, workspace, user_id, role, provisioned_by, display_name
):
    """The membership half of the saga: seat, row, emit, audit, one commit."""
    with transaction.atomic():
        # A provisioned member takes a seat like any other, so the seat is
        # reserved under the workspace lock (WORK-02) — provisioning used
        # to bypass the members.max ceiling entirely, which made it the
        # cheapest way for an org to grow past the plan it pays for.
        locked = lock_workspace(workspace)
        if locked is None:
            from .errors import ERR_404_WORKSPACE_NOT_FOUND

            raise ProvisionError(ERR_404_WORKSPACE_NOT_FOUND)
        verdict = check_org_entitlement(
            locked,
            ENT_MEMBERS_MAX,
            quantity=member_seats_quantity(locked, additional=1),
        )
        if not verdict.allowed:
            raise EntitlementDenied(ENT_MEMBERS_MAX, verdict)
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
        record_audit(
            workspace=workspace,
            action=AuditAction.MEMBER_PROVISIONED,
            actor=provisioned_by,
            subject=user_id,
            role=role,
        )
        operation.state = ProvisionState.COMPLETED
        operation.user_id = user_id
        operation.credits_to_refund = 0
        operation.last_error = ""
        operation.save(
            update_fields=["state", "user_id", "credits_to_refund", "last_error"]
        )
    return member


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
        # No actor: a suspension is applied by a POLICY (the require-MFA
        # sweep, the deactivation consumer), not by a person clicking. A
        # named actor here would be an invention.
        record_audit(
            workspace=member.workspace_id,
            action=AuditAction.MEMBER_SUSPENDED,
            subject=member.user_id,
            role=member.role,
            reason=reason,
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
        record_audit(
            workspace=member.workspace_id,
            action=AuditAction.MEMBER_UNSUSPENDED,
            subject=member.user_id,
            role=member.role,
            reason=lifted_reason,
        )
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


def mfa_enforcement_for(workspace: Workspace) -> WorkspaceMFAEnforcement:
    """The workspace's enforcement record, created ``pending`` if absent.

    Reading it is how any surface answers "is MFA actually enforced here" —
    the settings flag only says somebody asked for it.
    """
    record, _ = WorkspaceMFAEnforcement.objects.get_or_create(workspace=workspace)
    return record


def record_member_mfa(member: WorkspaceMember, *, compliant: bool) -> None:
    """Persist one member's compliance answer and act on it.

    Compliance is stored per member (``mfa_compliant`` / ``mfa_verified_at``)
    rather than inferred from "not suspended": a member nobody ever asked
    about and a member auth confirmed look identical from the suspension
    column, and telling them apart is the whole of WORK-01.
    """
    member.mfa_compliant = compliant
    member.mfa_verified_at = timezone.now()
    member.save(update_fields=["mfa_compliant", "mfa_verified_at"])
    if not compliant:
        suspend_member(member, reason=SUSPENSION_NO_MFA)


def verify_member_mfa(member: WorkspaceMember) -> bool | None:
    """Ask ``auth.mfa_status`` about one member and record the answer.

    Returns True/False as auth answered, or ``None`` when auth could not be
    reached — the honest third value the old sweep collapsed into "carry
    on". A None never marks the member compliant, so the admission gate
    keeps refusing until somebody gets a real answer.
    """
    try:
        status = call(MFA_STATUS, {"user_id": str(member.user_id)}) or {}
    except (
        FunctionNotRegistered,
        FunctionRouteNotConfigured,
        FunctionCallError,
    ) as exc:
        logger.warning(
            "auth.mfa_status unavailable (%s) — member %s stays unverified",
            exc,
            member.pk,
        )
        return None
    compliant = bool(status.get("has_strong_mfa"))
    record_member_mfa(member, compliant=compliant)
    return compliant


def enforce_require_mfa(workspace: Workspace) -> WorkspaceMFAEnforcement:
    """Sweep the workspace's members and record how far enforcement got.

    Asks ``auth.mfa_status`` for every active member, suspends those
    without a strong factor (reason ``no_mfa``, emit + letter) and writes
    what happened to :class:`~stapel_workspaces.models.WorkspaceMFAEnforcement`:
    ``enforced`` only when every member answered, ``failed`` (with
    ``last_error``) when auth broke, ``enforcing`` when coverage is
    otherwise incomplete.

    An auth outage no longer ends the sweep — the remaining members are
    still attempted, because one unreachable call is not a reason to leave
    the rest of the organization unexamined. It also no longer ends in
    silence: the record is what the retry sweep
    (:func:`retry_mfa_enforcement`), the administrator's screen and the
    admission gate all read, so a policy that did not finish cannot be
    reported as one that did.

    Members are NOT suspended on an auth error (unchanged, spec §C3:
    fail-closed there would lock out a whole org on a hiccup). They are
    instead left unverified, which the admission gate treats as "not
    admitted while the policy is on" — the containment moved from a blanket
    suspension to the door.

    Idempotent: re-running re-asks only what it must and rewrites the same
    record.
    """
    record = mfa_enforcement_for(workspace)
    members = list(
        WorkspaceMember.objects.active()
        .filter(workspace=workspace)
        .select_related("workspace", "user")
        .order_by("invited_at")
    )
    checked = 0
    noncompliant = 0
    unknown = 0
    last_error = ""
    for member in members:
        try:
            status = call(MFA_STATUS, {"user_id": str(member.user_id)}) or {}
        except (
            FunctionNotRegistered,
            FunctionRouteNotConfigured,
            FunctionCallError,
        ) as exc:
            unknown += 1
            last_error = f"{type(exc).__name__}: {exc}"
            logger.warning(
                "auth.mfa_status unavailable (%s) — member %s of workspace %s "
                "stays unverified and is not admitted while require_mfa is on",
                exc,
                member.pk,
                workspace.pk,
            )
            continue
        checked += 1
        compliant = bool(status.get("has_strong_mfa"))
        if not compliant:
            noncompliant += 1
        record_member_mfa(member, compliant=compliant)
    now = timezone.now()
    record.attempts += 1
    record.last_attempt_at = now
    record.checked_members = checked
    record.noncompliant_members = noncompliant
    record.last_error = last_error
    if last_error:
        record.state = MFAEnforcementState.FAILED
        record.completed_at = None
    elif unknown or _members_awaiting_mfa_verification(workspace).exists():
        record.state = MFAEnforcementState.ENFORCING
        record.completed_at = None
    else:
        record.state = MFAEnforcementState.ENFORCED
        record.completed_at = now
    record.save(
        update_fields=[
            "attempts",
            "last_attempt_at",
            "checked_members",
            "noncompliant_members",
            "last_error",
            "state",
            "completed_at",
        ]
    )
    return record


def _members_awaiting_mfa_verification(workspace: Workspace):
    """Active members of *workspace* whose factor nobody has confirmed."""
    return (
        WorkspaceMember.objects.active()
        .filter(workspace=workspace, mfa_compliant__isnull=True)
        .select_related("workspace", "user")
    )


def retry_mfa_enforcement(*, limit: int = 100) -> list:
    """The durable half: re-sweep every workspace that is not ``enforced``.

    Idempotent by construction — it re-reads state from the database and
    re-asks auth, so running it twice costs two calls and changes nothing
    else. A deployment schedules ``manage.py enforce_workspace_mfa``;
    without it the retry still happens lazily, one member at a time, at the
    admission gate.

    Returns the records it touched (newest attempt first is not promised;
    the order is the queue's).
    """
    pending = (
        WorkspaceMFAEnforcement.objects.exclude(state=MFAEnforcementState.ENFORCED)
        .filter(workspace__deleted_at__isnull=True)
        .select_related("workspace")
        .order_by("last_attempt_at")[:limit]
    )
    touched = []
    for record in pending:
        if not security_settings_for(record.workspace).require_mfa:
            # The policy was switched off while this row waited; the lift
            # already ran, so there is nothing left to enforce.
            continue
        touched.append(enforce_require_mfa(record.workspace))
    return touched


def mfa_admission_blocked(member: WorkspaceMember) -> bool:
    """Is this member barred from the workspace by its ``require_mfa`` policy?

    True when the workspace requires a strong second factor and this
    member's compliance is not established. An unverified member is asked
    about on the spot (one ``auth.mfa_status`` call, the answer persisted),
    so the gate heals itself as people arrive; while the answer cannot be
    got, the member stays out.

    This is the "enforce at every admission" half of WORK-01. Without it,
    ``require_mfa`` was enforced exactly once — during a sweep that could
    quietly cover none of the org — and never again for anyone who joined,
    was reinstated, or was missed.
    """
    if member.mfa_compliant:
        return False
    if not security_settings_for(member.workspace).require_mfa:
        return False
    return verify_member_mfa(member) is not True


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
    # Every stored compliance answer is dropped with the policy. Keeping
    # them would let a workspace that switched MFA off for a year switch it
    # back on and admit people on a year-old "yes" — the enforcement record
    # goes back to pending for the same reason.
    WorkspaceMember.objects.filter(workspace=workspace).update(
        mfa_compliant=None, mfa_verified_at=None
    )
    WorkspaceMFAEnforcement.objects.filter(workspace=workspace).update(
        state=MFAEnforcementState.PENDING,
        completed_at=None,
        checked_members=0,
        noncompliant_members=0,
        last_error="",
    )
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
            # The event IS the answer auth would give — record it as such,
            # so the admission gate does not spend a call re-asking.
            record_member_mfa(member, compliant=False)
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
        # Same reasoning as the disable consumer: auth just told us the
        # factor exists, so the membership is verified without a call.
        member.mfa_compliant = True
        member.mfa_verified_at = timezone.now()
        member.save(update_fields=["mfa_compliant", "mfa_verified_at"])
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


def set_preferred_workspace(*, user, workspace_id) -> WorkspaceMember | None:
    """Record the person's EXPLICIT choice of home workspace, or clear it.

    ``workspace_id=None`` clears; otherwise the target must be a workspace
    the caller ACTIVELY belongs to (``MembershipQuerySet.active`` — an
    invitation not yet accepted and a suspended membership are both "cannot
    open it", and pointing a client at a workspace it cannot open is the
    defect this whole axis exists to remove). Returns the newly preferred
    membership, or ``None`` when the choice was cleared.

    Raises :class:`WorkspaceMember.DoesNotExist` when the target is not an
    active membership of this user — deleted, never existed, and belongs to
    somebody else are deliberately indistinguishable to the caller.

    Clear-then-set inside one transaction, because the invariant is a
    partial unique constraint ("at most one preferred row per user") and two
    devices switching at the same moment would otherwise raise IntegrityError
    at whichever landed second.
    """
    with transaction.atomic():
        current = WorkspaceMember.objects.select_for_update().filter(
            user=user, is_preferred=True
        )
        if workspace_id is None:
            current.update(is_preferred=False)
            return None
        target = (
            WorkspaceMember.objects.active()
            .filter(user=user, workspace_id=workspace_id, workspace__deleted_at__isnull=True)
            .select_related("workspace")
            .first()
        )
        if target is None:
            raise WorkspaceMember.DoesNotExist(workspace_id)
        current.exclude(pk=target.pk).update(is_preferred=False)
        if not target.is_preferred:
            target.is_preferred = True
            target.save(update_fields=["is_preferred"])
        return target


def preferred_workspace_id_for(user) -> str:
    """The caller's stated home workspace as a string id, or "".

    Filtered through :meth:`MembershipQuerySet.active` and the workspace's
    own soft-delete, so a preference set before a suspension simply goes
    quiet while the suspension lasts and returns when it lifts — the flag
    itself is never touched by the lifecycle.
    """
    member = (
        WorkspaceMember.objects.active()
        .filter(user=user, is_preferred=True, workspace__deleted_at__isnull=True)
        .only("workspace_id")
        .first()
    )
    return str(member.workspace_id) if member else ""


def instance_owner_ids() -> set:
    """User ids of the instance's owners — the OWNERs of its default workspace.

    There is no separate "instance owner" role in this module, and inventing
    one would be a second authority to keep in sync with the first. The
    instance's default workspace (``DEFAULT_WORKSPACE_ID``) is already the
    deployment's declared centre; whoever owns it owns the deployment.

    Empty when no default workspace is configured, when it has been deleted,
    or when nobody actively owns it. Empty is a real answer, not a fallback:
    under the ``instance_owner`` creation policy it means nobody may create a
    workspace through the API, which is the safe reading of "the instance
    never said who is in charge" — and is what W002 warns about at boot.
    """
    configured = str(workspaces_settings.DEFAULT_WORKSPACE_ID or "").strip()
    if not configured:
        return set()
    try:
        uuid.UUID(configured)
    except (TypeError, ValueError):
        return set()
    return set(
        WorkspaceMember.objects.active()
        .filter(
            workspace_id=configured,
            role=Role.OWNER,
            workspace__deleted_at__isnull=True,
        )
        .values_list("user_id", flat=True)
    )


def can_create_workspace(user) -> bool:
    """May *user* create a workspace on this instance (``WORKSPACE_CREATE_POLICY``)?

    The one place the policy is evaluated, so the gate on ``POST /workspaces``
    and the ``can_create_workspace`` flag a client draws its "+ New space"
    control from can never disagree — a button that 403s and a missing button
    that should be there are the same defect from two sides.

    An anonymous account is refused under every policy: a workspace has an
    owner, and a throwaway session cannot be one (the same reason
    ``WorkspaceListCreateView.post`` states at its own guard).
    """
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_anonymous", False):
        return False
    policy = workspace_create_policy()
    if policy == CREATE_POLICY_OPEN:
        return True
    if policy == CREATE_POLICY_CLOSED:
        return False
    return user.pk in instance_owner_ids()
