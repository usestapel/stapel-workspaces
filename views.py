"""DRF views for the workspaces service.

Guest (anonymous session) stance
--------------------------------
With ``AUTH_ANONYMOUS`` on, a guest session is ``is_authenticated``, so a bare
``IsAuthenticated`` says nothing about whether guests belong on a view
(``stapel_core.adoption`` E001/W002). Every view here now states its answer,
and the shape of the answer follows from what this module is:

* **Everything scoped to a workspace is already closed to guests** — not by
  a permission class but by :func:`_capability_check` in the method body: a
  guest holds no ``WorkspaceMember`` row, so ``membership is None`` and the
  answer is 403 ``forbidden_workspace`` before any data is touched. The
  invitation views close the same way, by the email match (an anonymous
  account has no email; verifying one is exactly what flips
  ``is_anonymous`` off). Those views declare ``ANONYMOUS_DENIED`` — the
  declaration does not add the gate, it makes the existing gate readable
  from the class header instead of only from the method body.
* **``WorkspaceListCreateView`` is on the live guest path** and must stay
  open: an app header asks "which workspaces am I in?" for *every* session,
  guest included, to decide what to draw. For a guest the answer is an empty
  list — the truth, and cheaper than a 403 the header would have to special-
  case. Its POST half is a different question and is answered separately in
  the method (see there).
* **``RoleListView``** is deployment metadata (the role registry a
  ``RoleSelect`` renders), not anybody's data. Closing it would protect
  nothing and could only break a frontend.
"""

from uuid import UUID

from django.contrib.auth import get_user_model
from django.db.models import CharField, F, Q, Value
from django.db.models.functions import Coalesce, Concat, NullIf, Trim
from django.utils import timezone
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from stapel_core.comm.exceptions import (
    FunctionNotRegistered,
    FunctionRouteNotConfigured,
)
from stapel_core.core.language import parse_accept_language
from stapel_core.django.api.errors import (
    StapelErrorResponse,
    StapelResponse,
    error_403_forbidden,
)
from stapel_core.django.api.pagination import AnchorPagination
from stapel_core.django.api.permissions import (
    ANONYMOUS_ALLOWED,
    ANONYMOUS_DENIED,
    IsServiceRequest,
    IsStaffUser,
)
from stapel_core.django.openapi.schemas import StapelErrorSerializer
from stapel_core.django.workspaces import invalidate_membership_cache
from stapel_core.signals import workspace_member_changed
from stapel_core.verification import requires_verification

from . import audit, entitlements, services
from .capabilities import (
    BUILTIN_ROLES,
    capabilities_for,
    effective_capability_levels,
    effective_roles,
    role_exceeds_rank,
    role_has_capability,
)
from .dto import (
    AuditEventResponse,
    DisplayNameResponse,
    MFAEnforcementStatus,
    InvitationClaimResponse,
    InvitationPreviewResponse,
    InvitationResponse,
    MemberInviteResponse,
    MemberPasswordResetResponse,
    MemberResponse,
    PreferredWorkspaceResponse,
    ProvisionMemberResponse,
    RoleListResponse,
    RoleResponse,
    WorkspaceListResponse,
    WorkspaceResponse,
)
from .errors import (
    ERR_400_INVITATION_ALREADY_USED,
    ERR_400_INVITATION_DECLINED,
    ERR_400_INVITATION_EXPIRED,
    ERR_400_INVITATION_REVOKED,
    ERR_400_SLUG_TAKEN,
    ERR_402_ENTITLEMENT_REQUIRED,
    ERR_402_MEMBER_LIMIT_REACHED,
    ERR_403_FORBIDDEN_WORKSPACE,
    ERR_403_LAST_OWNER,
    ERR_403_MEMBERSHIP_SUSPENDED,
    ERR_403_MISSING_CAPABILITY,
    ERR_403_ROLE_EXCEEDS_INVITER_RANK,
    ERR_403_WORKSPACE_CREATION_CLOSED,
    ERR_404_INVITATION_NOT_FOUND,
    ERR_404_MEMBER_NOT_FOUND,
    ERR_404_WORKSPACE_NOT_FOUND,
    ERR_409_EMAIL_ALREADY_REGISTERED,
    ERR_429_INVITATION_GRANT_PENDING,
    ERR_429_INVITATION_RESEND_COOLDOWN,
    ERR_503_AUTH_UNAVAILABLE,
)
from .models import (
    InvitationStatus,
    Role,
    Workspace,
    WorkspaceInvitation,
    WorkspaceMember,
    WorkspaceType,
)
from .permissions import get_membership, require_role, role_at_least
from .serializers import (
    AuditEventResponseSerializer,
    DisplayNameResponseSerializer,
    DisplayNameUpdateRequestSerializer,
    InstanceShapeResponseSerializer,
    InternalPersonalWorkspaceResponseSerializer,
    InvitationAcceptRequestSerializer,
    InvitationClaimResponseSerializer,
    InvitationPreviewResponseSerializer,
    InvitationResponseSerializer,
    MemberInviteRequestSerializer,
    MemberInviteResponseSerializer,
    MemberPasswordResetRequestSerializer,
    MemberPasswordResetResponseSerializer,
    MemberResponseSerializer,
    MemberUpdateRequestSerializer,
    PreferredWorkspaceRequestSerializer,
    PreferredWorkspaceResponseSerializer,
    ProvisionMemberRequestSerializer,
    ProvisionMemberResponseSerializer,
    RoleListResponseSerializer,
    WorkspaceCreateRequestSerializer,
    WorkspaceListResponseSerializer,
    WorkspaceResponseSerializer,
    WorkspaceUpdateRequestSerializer,
)


class SerializerSeamsMixin:
    """Overridable serializer seams for API views.

    Subclasses (or downstream projects) can swap the request/response
    serializers without copying method bodies:

        class MyWorkspaceDetailView(WorkspaceDetailView):
            response_serializer_class = MyWorkspaceResponseSerializer
    """

    request_serializer_class = None
    response_serializer_class = None

    def get_request_serializer_class(self):
        return self.request_serializer_class

    def get_response_serializer_class(self):
        return self.response_serializer_class


def _capability_check(membership, capability: str):
    """403 mapping for the capability layer (org-program spec §A2/§C3).

    Not a member at all → the historical ``forbidden_workspace`` boundary;
    a SUSPENDED member → ``membership_suspended`` with the reason as a
    param (callers fetch the membership with ``include_suspended=True`` so
    the honest state is reportable — a bare not-a-member 403 would hide
    the self-serve fix from the no_mfa case); a member whose role lacks
    the capability → ``missing_capability``.
    """
    if membership is None:
        return StapelErrorResponse(403, ERR_403_FORBIDDEN_WORKSPACE)
    if membership.suspended_at is not None:
        return StapelErrorResponse(
            403,
            ERR_403_MEMBERSHIP_SUSPENDED,
            params={"reason": membership.suspension_reason},
        )
    if not role_has_capability(membership.role, capability):
        return StapelErrorResponse(
            403, ERR_403_MISSING_CAPABILITY, params={"capability": capability}
        )
    return None


def _rank_check(actor_role: str, target_role: str):
    """403 when *target_role* outranks *actor_role* (rank-gard).

    Shared by invite/role-change/provision — every write that hands a role
    to somebody. Deliberately does not special-case ``owner``: the
    hardcoded owner-only gates at each call site already forbid granting or
    touching ``owner`` outright, and this ceiling gives the SAME verdict for
    it too (owner's rank always exceeds a non-owner actor's) — the two
    checks agree rather than one silently depending on the other never
    having been called for that case.
    """
    if role_exceeds_rank(target_role, actor_role):
        return StapelErrorResponse(
            403, ERR_403_ROLE_EXCEEDS_INVITER_RANK, params={"role": target_role}
        )
    return None


def _workspace_owner_names(workspaces) -> dict:
    """``{str(owner_id): display_name}`` for a batch of workspaces.

    One profiles call for the whole list, not one per row: the picker draws the
    owner under every workspace name, and a request per row is the N+1 the
    batch endpoint exists to prevent. Best-effort exactly like
    ``_member_display_names`` — an owner with no profile is simply absent, and
    the DTO carries "" rather than an id.
    """
    return services._fetch_profile_display_names(ws.owner_id for ws in workspaces)


def _mfa_enforcement_to_dto(ws: Workspace) -> MFAEnforcementStatus | None:
    """The workspace's MFA enforcement state, or None when the policy is off.

    The honest answer to "is MFA required here" (WORK-01): the settings
    block records the administrator's wish, this records what the sweep
    actually achieved — including the members nobody has been able to ask
    about, who are the ones an administrator has to act on.
    """
    if not services.security_settings_for(ws).require_mfa:
        return None
    record = services.mfa_enforcement_for(ws)
    return MFAEnforcementStatus(
        state=record.state,
        attempts=record.attempts,
        checked_members=record.checked_members,
        noncompliant_members=record.noncompliant_members,
        unverified_members=ws.members.active()
        .filter(mfa_compliant__isnull=True)
        .count(),
        last_attempt_at=(
            record.last_attempt_at.isoformat() if record.last_attempt_at else None
        ),
        completed_at=record.completed_at.isoformat() if record.completed_at else None,
        last_error=record.last_error,
    )


def _workspace_to_dto(
    ws: Workspace,
    my_role: str | None = None,
    member_count: int | None = None,
    owner_names: dict | None = None,
    mfa_enforcement: MFAEnforcementStatus | None = None,
) -> WorkspaceResponse:
    if member_count is None:
        # active(), not accepted(): a suspended membership counts for
        # nothing anywhere else — access, comm, the seat bill — and this
        # display counter was the last place still counting it (0.10.0).
        # An org showing "5 members" while its plan is billed for 4 is
        # exactly the drift the shared predicate exists to prevent.
        member_count = ws.members.active().count()
    return WorkspaceResponse(
        id=ws.id,
        name=ws.name,
        slug=ws.slug,
        type=ws.type,
        owner_id=ws.owner_id,
        settings=ws.settings or {},
        storage_used_bytes=ws.storage_used_bytes,
        storage_limit_bytes=ws.storage_limit_bytes,
        member_count=member_count,
        my_role=my_role,
        created_at=ws.created_at.isoformat(),
        updated_at=ws.updated_at.isoformat(),
        my_capabilities=capabilities_for(my_role) if my_role else [],
        # `owner_names` is the batched answer when a caller has one (the list
        # endpoint); a single-workspace response resolves it on its own rather
        # than returning "" — a field that is populated on the list and empty
        # on the detail of the same workspace is the kind of inconsistency
        # clients paper over with a cache lookup.
        owner_display_name=(
            owner_names
            if owner_names is not None
            else _workspace_owner_names([ws])
        ).get(str(ws.owner_id), ""),
        # Only the single-workspace responses carry it: the list endpoint
        # would pay a per-row count for a block nothing on a switcher reads.
        mfa_enforcement=mfa_enforcement,
    )


def _member_to_dto(m: WorkspaceMember, display_name: str | None = None) -> MemberResponse:
    return MemberResponse(
        id=m.id,
        workspace_id=m.workspace_id,
        user_id=m.user_id,
        email=getattr(m.user, "email", None),
        role=m.role,
        invited_at=m.invited_at.isoformat(),
        accepted_at=m.accepted_at.isoformat() if m.accepted_at else None,
        last_accessed_at=m.last_accessed_at.isoformat() if m.last_accessed_at else None,
        provisioned=m.provisioned,
        suspended_at=m.suspended_at.isoformat() if m.suspended_at else None,
        suspension_reason=m.suspension_reason or None,
        display_name=display_name,
        mfa_compliant=m.mfa_compliant,
    )


def _member_display_names(members) -> dict:
    """Resolve display names for a batch of members (stapel-profiles first).

    stapel-profiles wins whenever it has a name (a live, canonical answer);
    ``display_name_hint`` — the name typed at invite/provision time — is the
    fallback for exactly the gap that leaves open: profiles not installed in
    this deployment, unreachable, or simply not having caught up yet. Never
    both, never invented when neither exists.
    """
    members = list(members)
    names = services._fetch_profile_display_names(m.user_id for m in members)
    for m in members:
        key = str(m.user_id)
        if not names.get(key) and m.display_name_hint:
            names[key] = m.display_name_hint
    return names


def _member_display_name_expr():
    """SQL expression for a member's display name.

    Mirrors how the member surface already *presents* a member — ``_member_to_dto``
    joins ``user`` and surfaces its identity — but resolves the name the way a
    people-picker shows it: prefer the user's full name, fall back to username,
    then email. Used for BOTH ``?search=`` matching and the stable ordering so
    every downstream multi-tenant project stops hand-rolling its own member
    listing (BACKLOG G12).
    """
    full_name = Trim(
        Concat(
            Coalesce(F("user__first_name"), Value("")),
            Value(" "),
            Coalesce(F("user__last_name"), Value("")),
        )
    )
    return Coalesce(
        NullIf(full_name, Value("")),
        F("user__username"),
        F("user__email"),
        Value(""),
        output_field=CharField(),
    )


def _invitation_state_error(inv: WorkspaceInvitation):
    """Shared 400 mapping for acting on a non-pending invitation.

    Used by accept, decline and claim so all three agree on the state
    machine and its precedence (revoked > accepted > declined > expired —
    the same order ``WorkspaceInvitation.status`` derives its label in).
    Returns ``None`` while the invitation is actionable (pending).
    """
    if inv.revoked_at:
        return StapelErrorResponse(400, ERR_400_INVITATION_REVOKED)
    if inv.accepted_at:
        return StapelErrorResponse(400, ERR_400_INVITATION_ALREADY_USED)
    if inv.declined_at:
        return StapelErrorResponse(400, ERR_400_INVITATION_DECLINED)
    if inv.expires_at and inv.expires_at < timezone.now():
        return StapelErrorResponse(400, ERR_400_INVITATION_EXPIRED)
    return None


def _mask_email(email: str) -> str:
    """Mask an email for the public invitation preview: ``m***@e***.com``.

    First character of the local part and of the domain name survive, the
    TLD stays readable — enough for the invitee to recognize their own
    address, useless for harvesting.
    """
    local, _, domain = email.partition("@")
    domain_name, dot, tld = domain.rpartition(".")
    masked_local = f"{local[:1] or '*'}***"
    if dot:
        return f"{masked_local}@{domain_name[:1] or '*'}***.{tld}"
    return f"{masked_local}@{domain[:1] or '*'}***"


def _email_registered(email: str) -> bool:
    """Whether an account already exists for the invited email (spec §B2)."""
    return get_user_model().objects.filter(email__iexact=email).exists()


class TokenPathNoLogMixin:
    """Keep the invite token out of the logs (org-program spec §B2).

    The invite-flow endpoints carry the bearer token in the URL path, and
    Django's request logging writes ``request.path`` for every 4xx/5xx
    response (``django.core.handlers.base`` → ``log_response``) — which
    would persist the secret in plaintext logs on any miss (404 probe,
    expired token, throttle 429...). ``log_response`` honours the
    documented ``_has_been_logged`` flag on the response; setting it here
    suppresses exactly that path log for these views. Runs in
    ``finalize_response`` so DRF-handled exceptions (401/403/429) are
    covered too. Module code never logs the token either.
    """

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        if response.status_code >= 400:
            response._has_been_logged = True
        return response


class InvitationThrottle(ScopedRateThrottle):
    """Enumeration backstop for the AllowAny invitation endpoints.

    The invite token is a bearer secret, but a public endpoint still must
    not be free to enumerate. DRF resolves scoped rates from the global
    ``DEFAULT_THROTTLE_RATES`` setting, which a library module cannot own —
    the rate is read from the module namespace instead
    (``STAPEL_WORKSPACES["INVITATION_THROTTLE"]``; the stapel-geo
    ``GeocodingThrottle`` canon). ``None`` disables throttling.
    """

    scope = "workspace-invitation"

    def get_rate(self):
        from .conf import workspaces_settings

        return workspaces_settings.INVITATION_THROTTLE


def _invitation_to_dto(inv: WorkspaceInvitation) -> InvitationResponse:
    return InvitationResponse(
        id=inv.id,
        workspace_id=inv.workspace_id,
        email=inv.email,
        role=inv.role,
        status=str(inv.status),
        expires_at=inv.expires_at.isoformat(),
        accepted_at=inv.accepted_at.isoformat() if inv.accepted_at else None,
        declined_at=inv.declined_at.isoformat() if inv.declined_at else None,
        revoked_at=inv.revoked_at.isoformat() if inv.revoked_at else None,
        created_at=inv.created_at.isoformat(),
        invited_by_id=inv.invited_by_id,
        display_name=inv.display_name_hint or None,
        revoked_by_id=inv.revoked_by_id,
        last_sent_at=inv.last_sent_at.isoformat() if inv.last_sent_at else None,
    )


def _resend_cooldown_response(retry_after: int):
    """429 for a resend inside the cooldown window.

    ``retry_after`` travels twice on purpose: as an error param (so the
    localized sentence can name the wait, and a client can count down) and
    as the standard ``Retry-After`` header, which is what generic HTTP
    clients, proxies and retry middleware already know how to read.
    """
    resp = StapelErrorResponse(
        429,
        ERR_429_INVITATION_RESEND_COOLDOWN,
        params={"retry_after": retry_after},
    )
    resp["Retry-After"] = str(retry_after)
    return resp


def _invitation_terminal_error(inv: WorkspaceInvitation):
    """State mapping for acting on an invitation the TTL may have caught.

    :func:`_invitation_state_error` minus the expiry clause. Used by resend,
    which exists precisely to revive an expired invitation: an expired row
    is a *delivery* failure, and the three stored timestamps are
    *decisions*. Returns ``None`` while the invitation is still unresolved
    — the same set ``InvitationQuerySet.unresolved()`` selects, so the
    view's answer and the service's compare-and-set agree on the boundary
    by construction.
    """
    if inv.revoked_at:
        return StapelErrorResponse(400, ERR_400_INVITATION_REVOKED)
    if inv.accepted_at:
        return StapelErrorResponse(400, ERR_400_INVITATION_ALREADY_USED)
    if inv.declined_at:
        return StapelErrorResponse(400, ERR_400_INVITATION_DECLINED)
    return None


@extend_schema(tags=["Workspaces"])
class WorkspaceListCreateView(SerializerSeamsMixin, APIView):
    permission_classes = [permissions.IsAuthenticated]
    # GET is a live guest path: an app header asks "which workspaces am I in?"
    # for every session, guest included, to decide what to draw (meettoday's
    # Navbar does exactly this). The answer for a guest is an empty list — its
    # own memberships, of which it has none. POST is a separate question and
    # is answered inside `post` itself; see the guard there.
    stapel_anonymous_access = ANONYMOUS_ALLOWED
    request_serializer_class = WorkspaceCreateRequestSerializer
    response_serializer_class = WorkspaceResponseSerializer
    list_response_serializer_class = WorkspaceListResponseSerializer

    def get_list_response_serializer_class(self):
        return self.list_response_serializer_class

    @extend_schema(responses={200: WorkspaceListResponseSerializer})
    def get(self, request):  # noqa: R007
        # Suspended memberships are excluded: suspension closes access to
        # the org entirely (spec §C3) — the workspace disappears from the
        # member's own list until the suspension lifts.
        memberships = (
            WorkspaceMember.objects.active()
            .filter(user=request.user)
            .select_related("workspace")
            .order_by("-last_accessed_at", "-invited_at")
        )
        live = [m for m in memberships if not m.workspace.deleted_at]
        # ONE profiles call for every owner in the list — see
        # `_workspace_owner_names`.
        owner_names = _workspace_owner_names(m.workspace for m in live)
        workspaces = [
            _workspace_to_dto(m.workspace, my_role=m.role, owner_names=owner_names)
            for m in live
        ]
        # Definitionally the same answer as permissions.is_guest(request.user)
        # (same active()+deleted_at__isnull filter this loop already applied)
        # — read off the list already fetched instead of a second query.
        # test_guest_predicate.py pins the two never drifting apart.
        #
        # The instance default is echoed back ONLY when this caller is
        # actually a member of it. A client told to open a workspace it
        # cannot open would trade one wrong screen for another — and the
        # membership list needed to decide that is right here, already
        # fetched.
        from .conf import workspaces_settings

        configured = str(workspaces_settings.DEFAULT_WORKSPACE_ID or "")
        # str() on both sides deliberately: `w.id` is a UUID and the setting
        # is a string, and `UUID(...) == "a8bb..."` is False in Python — the
        # comparison would have silently never matched, which is the same
        # shape of defect this whole key exists to remove.
        default_id = (
            configured
            if configured and any(str(w.id) == configured for w in workspaces)
            else ""
        )
        # The person's own stated choice, echoed on the same response the
        # client already fetches — no second round trip to learn where home
        # is, and no window in which the list has arrived but the choice has
        # not. Guarded by the same rule as the instance default: active
        # membership or "".
        preferred_id = (
            services.preferred_workspace_id_for(request.user)
            if workspaces
            else ""
        )
        return StapelResponse(
            self.get_list_response_serializer_class()(
                WorkspaceListResponse(
                    workspaces=workspaces,
                    is_guest=not workspaces,
                    default_workspace_id=default_id,
                    preferred_workspace_id=preferred_id,
                    # The instance's creation policy, ANSWERED FOR THIS
                    # CALLER — not the policy name, which would put the
                    # instance-owner lookup back on the client. It rides the
                    # list because the switcher is the surface that draws the
                    # "+ New space" control, and it already fetches this.
                    can_create_workspace=services.can_create_workspace(request.user),
                )
            )
        )

    @extend_schema(
        request=WorkspaceCreateRequestSerializer,
        responses={201: WorkspaceResponseSerializer},
    )
    def post(self, request):  # noqa: R007
        # The other half of this view's guest stance. Listing is open to a
        # guest (see the class header); creating is not. A workspace has an
        # owner, and `create_workspace` makes the caller it — for an ORG that
        # owner is also the billing anchor. An anonymous account is throwaway
        # by construction: nobody can ever log back into it, so the org would
        # outlive the only account that can administer or pay for it. The
        # entitlement seam below cannot catch this, because it degrades to
        # allow when billing is not installed, and a PERSONAL workspace is
        # never gated by it at all.
        if getattr(request.user, "is_anonymous", False):
            return error_403_forbidden()
        # WHO MAY FOUND A WORKSPACE ON THIS INSTANCE (WORKSPACE_CREATE_POLICY).
        # A public cloud answers "anyone"; a private one answers "the owner of
        # the cloud" — on an instance where entry is by invitation, a member
        # who could mint their own org would step outside the org they were
        # invited into. Evaluated by the same helper the `can_create_workspace`
        # flag on the LIST response is drawn from, so the button a client shows
        # and the door it opens can never disagree.
        if not services.can_create_workspace(request.user):
            return StapelErrorResponse(403, ERR_403_WORKSPACE_CREATION_CLOSED)
        ser = self.get_request_serializer_class()(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data
        slug = getattr(data, "slug", None)
        if slug and Workspace.objects.filter(slug=slug).exists():
            return StapelErrorResponse(400, ERR_400_SLUG_TAKEN)
        # Entitlement seam (spec §D2): creating an ORGANIZATION (type=work)
        # is plan-gated on the creator — the would-be owner and billing
        # anchor. Personal workspaces are never gated. Without billing
        # installed the check degrades to allow.
        if (data.type or WorkspaceType.WORK) == WorkspaceType.WORK:
            verdict = entitlements.check_entitlement(
                request.user.pk, entitlements.ENT_ORG
            )
            if not verdict.allowed:
                return StapelErrorResponse(402, ERR_402_ENTITLEMENT_REQUIRED)
        ws = services.create_workspace(
            user=request.user,
            name=data.name,
            slug=slug,
            type=data.type or "work",
        )
        return StapelResponse(
            self.get_response_serializer_class()(
                _workspace_to_dto(ws, my_role=Role.OWNER)
            ),
            status=status.HTTP_201_CREATED,
        )


@extend_schema(tags=["Workspaces"])
class PreferredWorkspaceView(SerializerSeamsMixin, APIView):
    """``PUT/DELETE me/preferred-workspace`` — the person states where home is.

    ``STAPEL_WORKSPACES["DEFAULT_WORKSPACE_ID"]`` already describes itself as
    "a DEFAULT, not a cage: a person still switches spaces, and their
    explicit choice wins over it" — and until this endpoint there was nowhere
    for that choice to be written down. Clients filled the hole by guessing,
    and the guess (``workspaces[0]`` off a recency-ordered list) is #239.

    The choice is stated, never inferred. ``last_accessed_at`` remains what
    it has always been: telemetry written as a side effect of a GET, used to
    sort the list and for nothing else.

    Deliberately user-scoped rather than workspace-scoped
    (``.../<workspace_id>/prefer``): there is exactly one answer per person,
    and a route shaped per workspace would invite a second one.
    """

    permission_classes = [permissions.IsAuthenticated]
    # A guest holds no WorkspaceMember row anywhere, so the active-membership
    # lookup below finds nothing and answers the same 404 as any other
    # non-member — before a row of anybody's is written. The declaration does
    # not add the gate; it makes it readable from the class header.
    stapel_anonymous_access = ANONYMOUS_DENIED
    request_serializer_class = PreferredWorkspaceRequestSerializer
    response_serializer_class = PreferredWorkspaceResponseSerializer

    @extend_schema(
        request=PreferredWorkspaceRequestSerializer,
        responses={200: PreferredWorkspaceResponseSerializer},
    )
    def put(self, request):  # noqa: R007
        ser = self.get_request_serializer_class()(data=request.data)
        ser.is_valid(raise_exception=True)
        try:
            member = services.set_preferred_workspace(
                user=request.user, workspace_id=ser.validated_data.workspace_id
            )
        except WorkspaceMember.DoesNotExist:
            # One identical answer for "does not exist", "you are not in it",
            # "your invitation is still pending" and "you are suspended".
            # Distinguishing them would let anyone probe the instance for
            # which workspace ids are real.
            return StapelErrorResponse(404, ERR_404_WORKSPACE_NOT_FOUND)
        return StapelResponse(
            self.get_response_serializer_class()(
                PreferredWorkspaceResponse(
                    preferred_workspace_id=str(member.workspace_id)
                )
            )
        )

    @extend_schema(responses={200: PreferredWorkspaceResponseSerializer})
    def delete(self, request):  # noqa: R007
        """Clear the choice — back to the instance default / the client's chain.

        Answers 200 with an empty id rather than 204: the client's whole job
        here is to re-resolve, and handing it the new state saves it from
        guessing what "no content" left behind. Idempotent.
        """
        services.set_preferred_workspace(user=request.user, workspace_id=None)
        return StapelResponse(
            self.get_response_serializer_class()(PreferredWorkspaceResponse())
        )


@extend_schema(tags=["Workspaces"])
class WorkspaceDetailView(SerializerSeamsMixin, APIView):
    permission_classes = [permissions.IsAuthenticated]
    # Gated in the body by `_resolve` → `_capability_check` (workspace.view /
    # workspace.update / workspace.delete). A guest holds no membership row,
    # so `membership is None` → 403 forbidden_workspace. Declared rather than
    # enforced by a permission class so the answer stays the keyed 403 the
    # frontend routes on, instead of an anonymous permission refusal.
    stapel_anonymous_access = ANONYMOUS_DENIED
    request_serializer_class = WorkspaceUpdateRequestSerializer
    response_serializer_class = WorkspaceResponseSerializer

    def _resolve(self, request, workspace_id, capability: str = "workspace.view"):
        ws = Workspace.objects.filter(id=workspace_id, deleted_at__isnull=True).first()
        if not ws:
            return None, None, StapelErrorResponse(404, ERR_404_WORKSPACE_NOT_FOUND)
        membership = get_membership(ws.id, request.user.id, include_suspended=True)
        err = _capability_check(membership, capability)
        if err:
            return None, None, err
        return ws, membership, None

    @extend_schema(responses={200: WorkspaceResponseSerializer})
    def get(self, request, workspace_id):  # noqa: R007
        ws, membership, err = self._resolve(request, workspace_id)
        if err:
            return err
        membership.last_accessed_at = timezone.now()
        membership.save(update_fields=["last_accessed_at"])
        return StapelResponse(
            self.get_response_serializer_class()(
                _workspace_to_dto(
                    ws,
                    my_role=membership.role,
                    mfa_enforcement=_mfa_enforcement_to_dto(ws),
                )
            )
        )

    @extend_schema(
        request=WorkspaceUpdateRequestSerializer,
        responses={200: WorkspaceResponseSerializer},
    )
    def patch(self, request, workspace_id):  # noqa: R007
        ws, membership, err = self._resolve(request, workspace_id, "workspace.update")
        if err:
            return err
        ser = self.get_request_serializer_class()(data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data
        # A settings payload carrying the `security` block is the HIGH
        # surface of this endpoint (spec §C3): extra capability gate
        # (workspace.security.manage) + step-up on the delegate method —
        # ordinary PATCHes (name/slug/other settings) stay step-up-free.
        if "security" in ((getattr(data, "settings", None)) or {}):
            err = _capability_check(membership, "workspace.security.manage")
            if err:
                return err
            return self._patch_security(request, ws, membership, data)
        return self._apply_patch(ws, membership, data)

    @requires_verification(scope="sensitive")
    def _patch_security(self, request, ws, membership, data):
        """Step-up-guarded branch: the PATCH touches the security block.

        Flipping ``require_mfa`` ON runs the sweep (``auth.mfa_status`` per
        member; no strong factor → suspension with reason ``no_mfa``) and
        answers with what the sweep ACHIEVED, in ``mfa_enforcement``: a
        member auth could not be asked about leaves the workspace
        ``enforcing``/``failed``, not ``enforced``, and stays out at the
        door (``permissions.get_membership``) until somebody gets an
        answer. Reporting a 200 as "MFA is now required" while half the
        organization had never been checked is WORK-01, and the response
        body is where it was invisible. Flipping the policy OFF lifts the
        ``no_mfa`` suspensions it caused and forgets the stored answers.
        """
        was_require_mfa = services.security_settings_for(ws).require_mfa
        response = self._apply_patch(ws, membership, data)
        if response.status_code != status.HTTP_200_OK:
            return response
        now_require_mfa = services.security_settings_for(ws).require_mfa
        if now_require_mfa and not was_require_mfa:
            services.enforce_require_mfa(ws)
        elif was_require_mfa and not now_require_mfa:
            services.lift_no_mfa_suspensions(ws)
        return StapelResponse(
            self.get_response_serializer_class()(
                _workspace_to_dto(
                    ws,
                    my_role=membership.role,
                    mfa_enforcement=_mfa_enforcement_to_dto(ws),
                )
            )
        )

    def _apply_patch(self, ws, membership, data):
        new_slug = getattr(data, "slug", None)
        if new_slug and new_slug != ws.slug:
            if Workspace.objects.filter(slug=new_slug).exclude(id=ws.id).exists():
                return StapelErrorResponse(400, ERR_400_SLUG_TAKEN)
            ws.slug = new_slug
        if getattr(data, "name", None):
            ws.name = data.name
        if getattr(data, "settings", None) is not None:
            ws.settings = data.settings
        ws.save()
        return StapelResponse(
            self.get_response_serializer_class()(
                _workspace_to_dto(ws, my_role=membership.role)
            )
        )

    @extend_schema(responses={204: None})
    def delete(self, request, workspace_id):  # noqa: R007
        ws, membership, err = self._resolve(request, workspace_id)
        if err:
            return err
        if not role_at_least(membership.role, Role.OWNER):
            return StapelErrorResponse(403, ERR_403_FORBIDDEN_WORKSPACE)
        ws.deleted_at = timezone.now()
        ws.save(update_fields=["deleted_at"])
        return StapelResponse(status=status.HTTP_204_NO_CONTENT)


class MemberPagination(AnchorPagination):
    """Anchor pagination for the member list.

    Workspace members carry no ``created_at``; ``invited_at`` (``auto_now_add``)
    IS the membership's creation timestamp — the direct analog of the ETALON
    modules' ``CreatedAtAnchorPagination`` (stapel-notifications /
    stapel-tasks). ``AnchorPagination`` supports only a single monotonic anchor
    (no composite ``name,id``), so the former display-name sort is dropped in
    favour of this stable, insertion-safe anchor: cursor windows must not shift
    under concurrent writes (stapel-core mandate; CHANGELOG 0.4.0).
    """

    anchor_field = "invited_at"
    ordering = "-invited_at"
    page_size = 100
    max_page_size = 500


@extend_schema(tags=["Members"])
class MemberListView(SerializerSeamsMixin, APIView):
    permission_classes = [permissions.IsAuthenticated]
    # `members.view` capability check in the body; a guest is not a member of
    # anything, so it never reaches the roster. Same reasoning as
    # WorkspaceDetailView.
    stapel_anonymous_access = ANONYMOUS_DENIED
    pagination_class = MemberPagination
    response_serializer_class = MemberResponseSerializer

    @extend_schema(
        responses={200: MemberResponseSerializer(many=True)},
        parameters=[
            OpenApiParameter(
                name="search",
                type=str,
                location=OpenApiParameter.QUERY,
                required=False,
                description=(
                    "Case-insensitive substring filter matched against a "
                    "member's email OR display name (full name / username). "
                    "Lets a people-picker filter server-side instead of "
                    "pulling every member."
                ),
            ),
        ],
    )
    def get(self, request, workspace_id):  # noqa: R007
        # List workspace members, anchor-paginated (stapel-core mandate:
        # limit/offset windows are forbidden — they slip rows under concurrent
        # writes). The paginator emits anchor/limit/direction and orders by the
        # -invited_at cursor. (No docstring here on purpose: drf-spectacular
        # turns a method docstring into the OpenAPI operation description, which
        # would break this module's byte-identity with the monolith contract
        # slice.)
        #   * search — case-insensitive substring on email OR display name
        #              (full name / username); lets a people-picker filter
        #              server-side instead of pulling every member (BACKLOG G12).
        err = _capability_check(
            get_membership(workspace_id, request.user.id, include_suspended=True),
            "members.view",
        )
        if err:
            return err
        members = (
            WorkspaceMember.objects.filter(workspace_id=workspace_id)
            .select_related("user")
            .annotate(_display_name=_member_display_name_expr())
        )
        search = (request.query_params.get("search") or "").strip()
        if search:
            members = members.filter(
                Q(_display_name__icontains=search)
                | Q(user__email__icontains=search)
            )
        paginator = MemberPagination()
        page = paginator.paginate_queryset(members, request)
        response_cls = self.get_response_serializer_class()
        names = _member_display_names(page)
        items = [
            response_cls(_member_to_dto(m, names.get(str(m.user_id)))).data
            for m in page
        ]
        return paginator.get_paginated_response(items)


@extend_schema(tags=["Members"])
class MemberInviteView(SerializerSeamsMixin, APIView):
    permission_classes = [permissions.IsAuthenticated]
    # `members.invite` capability check in the body — a guest has no
    # membership and therefore no role that could carry it.
    stapel_anonymous_access = ANONYMOUS_DENIED
    request_serializer_class = MemberInviteRequestSerializer
    response_serializer_class = MemberInviteResponseSerializer

    @extend_schema(
        request=MemberInviteRequestSerializer,
        responses={201: MemberInviteResponseSerializer},
    )
    def post(self, request, workspace_id):  # noqa: R007
        ws = Workspace.objects.filter(id=workspace_id, deleted_at__isnull=True).first()
        if not ws:
            return StapelErrorResponse(404, ERR_404_WORKSPACE_NOT_FOUND)
        membership = get_membership(ws.id, request.user.id, include_suspended=True)
        err = _capability_check(membership, "members.invite")
        if err:
            return err
        ser = self.get_request_serializer_class()(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data
        # Rank-gard (mandate-model vardict 2026-08-03): `members.invite`
        # says "may invite at all", not "up to which rank" — a role below
        # admin that also carries the capability must not hand out a rank
        # above its own holder's.
        err = _rank_check(membership.role, data.role)
        if err:
            return err
        # Entitlement seam (spec §D2): capability first ("may YOU", 403),
        # then the org's plan ceiling ("may the ORG", 402). Seats = accepted
        # + pending live invitations + the invitations about to be created,
        # counted and taken in ONE locked transaction (WORK-02) — a check
        # here and a write afterwards is a seat two batches can both sell.
        try:
            invitations = services.invite_members(
                workspace=ws,
                emails=data.emails,
                role=data.role,
                invited_by=request.user,
                display_name=getattr(data, "display_name", None),
            )
        except entitlements.EntitlementDenied as denied:
            limit = denied.result.limit
            return StapelErrorResponse(
                402,
                ERR_402_MEMBER_LIMIT_REACHED,
                params={"limit": limit if limit is not None else 0},
            )
        except Workspace.DoesNotExist:
            return StapelErrorResponse(404, ERR_404_WORKSPACE_NOT_FOUND)
        return StapelResponse(
            self.get_response_serializer_class()(
                MemberInviteResponse(
                    invitations=[_invitation_to_dto(i) for i in invitations]
                )
            ),
            status=status.HTTP_201_CREATED,
        )


class InvitationPagination(AnchorPagination):
    """Anchor pagination for the workspace invitation list (#109).

    ``created_at`` (``auto_now_add``) is the invitation's own creation
    timestamp — the same ETALON ``CreatedAtAnchorPagination`` shape the
    member list uses over ``invited_at``. Anchor, not limit/offset
    (stapel-core mandate): an admin working through a long pending list
    while invites are still being sent must not have rows slip under the
    window.
    """

    anchor_field = "created_at"
    ordering = "-created_at"
    page_size = 100
    max_page_size = 500


#: ``status`` filter values of the invitation list → the canonical
#: predicate each one means. The predicates live in
#: :class:`~stapel_workspaces.models.InvitationQuerySet` and are the ONLY
#: place the lifecycle columns are spelled (``tests/
#: test_lifecycle_predicates.py`` fails the build on a hand-written copy) —
#: this map is a vocabulary for the wire, not a second definition.
INVITATION_LIST_FILTERS = {
    # The default and the reason this endpoint exists: live, actionable,
    # seat-reserving invitations — "who has not accepted yet".
    "pending": lambda qs: qs.pending(),
    # Everything that never became a membership, for ANY reason: pending
    # plus declined, revoked and expired. The audit view of the same
    # question — an admin chasing a hire wants to see the decline.
    "never_accepted": lambda qs: qs.never_accepted(),
    # The full history of the workspace, accepted rows included.
    "all": lambda qs: qs,
}


@extend_schema(tags=["Members"])
class WorkspaceInvitationListView(SerializerSeamsMixin, APIView):
    """Who was invited and has not accepted (#109).

    Before this endpoint an admin could send invitations and never see
    them again: acceptance produced a member row, and everything else —
    the unopened mail, the wrong address, the person who declined — was
    invisible from the product. The roster answered "who is in"; nothing
    answered "who is still out, and since when".

    Gated on ``members.invite``: the mandate that creates invitations is
    the mandate that sees and manages them. Reading the list is strictly
    less than sending one, so no separate capability is minted — a role
    that may invite but may not audit its own invitations would be a
    distinction without a use.

    The invite token is never in the response. It is a bearer credential;
    a list endpoint that carried it would hand every admin a working login
    link for every invited address.
    """

    permission_classes = [permissions.IsAuthenticated]
    # `members.invite` capability check in the body — a guest holds no
    # membership and therefore no role that could carry it.
    stapel_anonymous_access = ANONYMOUS_DENIED
    pagination_class = InvitationPagination
    response_serializer_class = InvitationResponseSerializer

    @extend_schema(
        responses={200: InvitationResponseSerializer(many=True)},
        parameters=[
            OpenApiParameter(
                name="status",
                type=str,
                location=OpenApiParameter.QUERY,
                required=False,
                enum=sorted(INVITATION_LIST_FILTERS),
                description=(
                    "Which invitations to return. `pending` (default) — "
                    "live, actionable, seat-reserving ones, i.e. who has "
                    "not accepted yet. `never_accepted` — those plus the "
                    "declined, revoked and expired ones. `all` — the full "
                    "history, accepted rows included."
                ),
            ),
            OpenApiParameter(
                name="search",
                type=str,
                location=OpenApiParameter.QUERY,
                required=False,
                description=(
                    "Case-insensitive substring filter on the invited "
                    "email address."
                ),
            ),
        ],
    )
    def get(self, request, workspace_id):  # noqa: R007
        # (No docstring on the method on purpose: drf-spectacular turns a
        # method docstring into the OpenAPI operation description, and the
        # class docstring above is already the operation's description.)
        ws = Workspace.objects.filter(id=workspace_id, deleted_at__isnull=True).first()
        if not ws:
            return StapelErrorResponse(404, ERR_404_WORKSPACE_NOT_FOUND)
        err = _capability_check(
            get_membership(ws.id, request.user.id, include_suspended=True),
            "members.invite",
        )
        if err:
            return err
        wanted = (request.query_params.get("status") or "pending").strip()
        predicate = INVITATION_LIST_FILTERS.get(wanted)
        if predicate is None:
            return StapelErrorResponse(
                400, "error.400.field.invalid_choice", params={"field": "status"}
            )
        invitations = predicate(ws.invitations.all())
        search = (request.query_params.get("search") or "").strip()
        if search:
            invitations = invitations.filter(email__icontains=search)
        paginator = self.pagination_class()
        page = paginator.paginate_queryset(invitations, request)
        response_cls = self.get_response_serializer_class()
        items = [response_cls(_invitation_to_dto(i)).data for i in page]
        return paginator.get_paginated_response(items)


class WorkspaceInvitationActionView(SerializerSeamsMixin, APIView):
    """Shared resolution for the admin-side invitation actions (#109).

    Both actions answer with the SAME 404 for an unknown invitation UUID
    and for a real invitation belonging to a different workspace: the
    invitation id is scoped to the workspace in the URL, and an admin of
    one org must not be able to probe another org's invitation ids by the
    shape of the error.
    """

    permission_classes = [permissions.IsAuthenticated]
    # Capability check in `_resolve`; a guest has no membership, so 403
    # before any invitation row is read.
    stapel_anonymous_access = ANONYMOUS_DENIED
    response_serializer_class = InvitationResponseSerializer

    #: Which mandate this action demands. `members.invite` for the actions
    #: that create or end an invitation (revoke, resend); the name-edit
    #: PATCH overrides it — see InvitationNameView for why renaming is the
    #: members mandate rather than the invite one.
    capability = "members.invite"

    def _resolve(self, request, workspace_id, invitation_id):
        ws = Workspace.objects.filter(id=workspace_id, deleted_at__isnull=True).first()
        if not ws:
            return None, None, StapelErrorResponse(404, ERR_404_WORKSPACE_NOT_FOUND)
        err = _capability_check(
            get_membership(ws.id, request.user.id, include_suspended=True),
            self.capability,
        )
        if err:
            return None, None, err
        inv = WorkspaceInvitation.objects.filter(
            pk=invitation_id, workspace_id=ws.id
        ).first()
        if inv is None:
            return None, None, StapelErrorResponse(404, ERR_404_INVITATION_NOT_FOUND)
        return ws, inv, None


@extend_schema(tags=["Members"])
class InvitationRevokeView(WorkspaceInvitationActionView):
    """Withdraw a live invitation (#109).

    The workspace's terminal "no", the mirror of the invitee's decline —
    the two stay distinguishable in ``status`` forever. The seat the
    invitation reserved is freed on commit.

    Only a **pending** invitation is revocable: an accepted one produced a
    membership (remove the member instead), a declined or already-revoked
    one is terminal, and an expired one is already harmless. Each of those
    gets its own error key rather than a shrug.

    The transition is a compare-and-set under a row lock, not a blind
    write: an accept committing between this view's state check and the
    lock wins, and the revocation then fails honestly with
    ``error.400.invitation_already_used``. The reverse — a revocation
    committing between an accept's check and its lock — is the race fixed
    in 0.10.0, and it is the same predicate holding both ends.
    """

    @extend_schema(request=None, responses={200: InvitationResponseSerializer})
    def post(self, request, workspace_id, invitation_id):  # noqa: R007
        ws, inv, err = self._resolve(request, workspace_id, invitation_id)
        if err:
            return err
        err = _invitation_state_error(inv)
        if err:
            return err
        try:
            inv = services.revoke_invitation(invitation=inv, revoked_by=request.user)
        except ValueError:
            # Lost the compare-and-set: a terminal transition committed
            # between the check above and the row lock.
            inv.refresh_from_db()
            return _invitation_state_error(inv) or StapelErrorResponse(
                400, ERR_400_INVITATION_ALREADY_USED
            )
        return StapelResponse(
            self.get_response_serializer_class()(_invitation_to_dto(inv))
        )


@extend_schema(tags=["Members"])
class InvitationResendView(WorkspaceInvitationActionView):
    """Send the invitation email again (#109).

    Accepts an **expired** invitation on purpose — a dead TTL is the most
    common reason to resend — and refuses the three stored terminal states,
    which are decisions rather than delivery failures. The TTL restarts, so
    the invitee has the full window again from the letter that just went
    out.

    Reviving an expired invitation re-reserves a seat, so the plan ceiling
    is re-checked here exactly as it is on invite: capability first ("may
    YOU", 403), then the org's plan ("may the ORG", 402). An invitation
    that is already pending costs no additional seat and is never blocked
    by that check.

    Then the cooldown ("may we mail this PERSON again yet", 429). It is
    read here and enforced again inside the service's row lock, the same
    two-level shape the state check already has: the view answers a number
    the admin can read, the lock is what actually makes two simultaneous
    presses send one letter.

    The token is NOT rotated by default from 0.23 — the invitee's existing
    link keeps working, because the resend goes to the same mailbox that
    link is already sitting in. See
    ``STAPEL_WORKSPACES["INVITATION_ROTATE_TOKEN_ON_RESEND"]``.
    """

    @extend_schema(request=None, responses={200: InvitationResponseSerializer})
    def post(self, request, workspace_id, invitation_id):  # noqa: R007
        ws, inv, err = self._resolve(request, workspace_id, invitation_id)
        if err:
            return err
        err = _invitation_terminal_error(inv)
        if err:
            return err
        # A live invitation already occupies its seat; a revived expired one
        # does not yet, so it costs one more.
        additional = 0 if inv.status == InvitationStatus.PENDING else 1
        verdict = entitlements.check_org_entitlement(
            ws,
            entitlements.ENT_MEMBERS_MAX,
            quantity=entitlements.member_seats_quantity(ws, additional=additional),
        )
        if not verdict.allowed:
            return StapelErrorResponse(
                402,
                ERR_402_MEMBER_LIMIT_REACHED,
                params={"limit": verdict.limit if verdict.limit is not None else 0},
            )
        remaining = services.resend_cooldown_remaining(inv)
        if remaining:
            return _resend_cooldown_response(remaining)
        try:
            inv = services.resend_invitation(invitation=inv)
        except entitlements.EntitlementDenied as denied:
            # Lost the seat race: the plan filled up between the check
            # above and the reservation inside the lock.
            limit = denied.result.limit
            return StapelErrorResponse(
                402,
                ERR_402_MEMBER_LIMIT_REACHED,
                params={"limit": limit if limit is not None else 0},
            )
        except services.InvitationResendCooldown as exc:
            # Lost the other race the lock guards: a concurrent resend
            # claimed the window between the read above and the lock.
            return _resend_cooldown_response(exc.retry_after)
        except ValueError:
            inv.refresh_from_db()
            return _invitation_terminal_error(inv) or StapelErrorResponse(
                400, ERR_400_INVITATION_ALREADY_USED
            )
        return StapelResponse(
            self.get_response_serializer_class()(_invitation_to_dto(inv))
        )


@extend_schema(tags=["Members"])
class MemberProvisionView(SerializerSeamsMixin, APIView):
    """Provision an org-created (synthetic) member (org-program spec §C1).

    The org mints its own login/password account: full username is
    ``{workspace_slug}/{username_local}`` (namespaced — the slug is
    globally unique, so orgs cannot collide), the account is created by
    ``auth.provision_user`` with the workspace's first-login policy
    (``settings.security.provisioned_user_policy``: forced password change
    or mandatory MFA enrollment) and joins immediately
    (``accepted_at=now``, ``provisioned=True``).

    Gate stack, in order: HIGH step-up (``@requires_verification``, scope
    ``sensitive`` — the same store as admin step-up) → capability
    ``members.provision`` (403) → entitlement ``workspaces.provision_user``
    (402; degrades to allow without billing) → optional per-user debit
    (``STAPEL_WORKSPACES["PROVISION_USER_CREDITS"]`` > 0).

    Credentials: a synthetic account normally has no email — when the
    request omits ``email``, the server-generated password comes back in
    the response (``generated_password``) exactly once and no letter is
    sent. With ``email``, the ``workspace.provisioned_account`` letter
    carries the credentials as well. Auth's structured failures pass
    through keyed (``error.409.username_taken`` / auth's 400s); auth not
    wired → honest 503, this seam never degrades to allow.
    """

    permission_classes = [permissions.IsAuthenticated]
    # The strictest surface in this module, and a guest fails it twice over:
    # `@requires_verification(scope="sensitive")` demands a HIGH step-up an
    # anonymous account has no factor to satisfy, and `members.provision`
    # demands a membership it does not have.
    stapel_anonymous_access = ANONYMOUS_DENIED
    request_serializer_class = ProvisionMemberRequestSerializer
    response_serializer_class = ProvisionMemberResponseSerializer

    @extend_schema(
        request=ProvisionMemberRequestSerializer,
        responses={201: ProvisionMemberResponseSerializer},
    )
    @requires_verification(scope="sensitive")
    def post(self, request, workspace_id):  # noqa: R007
        ws = Workspace.objects.filter(id=workspace_id, deleted_at__isnull=True).first()
        if not ws:
            return StapelErrorResponse(404, ERR_404_WORKSPACE_NOT_FOUND)
        membership = get_membership(ws.id, request.user.id, include_suspended=True)
        err = _capability_check(membership, "members.provision")
        if err:
            return err
        ser = self.get_request_serializer_class()(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data
        # Rank-gard (mandate-model vardict 2026-08-03): `members.provision`
        # says "may provision at all", not "up to which rank" — same ceiling
        # as invite/role-change.
        err = _rank_check(membership.role, data.role)
        if err:
            return err
        # Entitlement seam (spec §D2): capability first ("may YOU", 403),
        # then the org's plan ("may the ORG", 402). Boolean key — plans
        # either include org-provisioned users or not.
        verdict = entitlements.check_org_entitlement(
            ws, entitlements.ENT_PROVISION_USER
        )
        if not verdict.allowed:
            return StapelErrorResponse(402, ERR_402_ENTITLEMENT_REQUIRED)
        try:
            member, username, generated_password = services.provision_member(
                workspace=ws,
                username_local=data.username_local,
                role=data.role,
                provisioned_by=request.user,
                password=getattr(data, "password", None),
                display_name=getattr(data, "display_name", None),
                email=getattr(data, "email", None),
            )
        except entitlements.EntitlementDenied:
            # The per-user debit was refused (e.g. insufficient credits).
            return StapelErrorResponse(402, ERR_402_ENTITLEMENT_REQUIRED)
        except services.ProvisionError as failure:
            # Structured auth failure, passed through keyed — the status
            # is encoded in the key itself (error.<status>.<name>).
            return StapelErrorResponse(
                _status_of_error_key(failure.error_key), failure.error_key
            )
        except (FunctionNotRegistered, FunctionRouteNotConfigured):
            return StapelErrorResponse(503, ERR_503_AUTH_UNAVAILABLE)
        return StapelResponse(
            self.get_response_serializer_class()(
                ProvisionMemberResponse(
                    user_id=member.user_id,
                    username=username,
                    role=member.role,
                    generated_password=generated_password,
                )
            ),
            status=status.HTTP_201_CREATED,
        )


def _status_of_error_key(error_key: str, default: int = 400) -> int:
    """HTTP status encoded in a canonical ``error.<status>.<name>`` key."""
    try:
        return int(error_key.split(".")[1])
    except (IndexError, ValueError):
        return default


@extend_schema(tags=["Members"])
class MemberDetailView(SerializerSeamsMixin, APIView):
    permission_classes = [permissions.IsAuthenticated]
    # `members.role.change` / `members.remove` capability checks in
    # `_resolve`; a guest has no membership, so 403 before any member row is
    # read.
    stapel_anonymous_access = ANONYMOUS_DENIED
    request_serializer_class = MemberUpdateRequestSerializer
    response_serializer_class = MemberResponseSerializer

    def _resolve(self, request, workspace_id, user_id, capability):
        err = _capability_check(
            get_membership(workspace_id, request.user.id, include_suspended=True),
            capability,
        )
        if err:
            return None, err
        member = WorkspaceMember.objects.filter(
            workspace_id=workspace_id, user_id=user_id
        ).first()
        if not member:
            return None, StapelErrorResponse(404, ERR_404_MEMBER_NOT_FOUND)
        return member, None

    @extend_schema(
        request=MemberUpdateRequestSerializer,
        responses={200: MemberResponseSerializer},
    )
    def patch(self, request, workspace_id, user_id):  # noqa: R007
        member, err = self._resolve(
            request, workspace_id, user_id, "members.role.change"
        )
        if err:
            return err
        ser = self.get_request_serializer_class()(data=request.data)
        ser.is_valid(raise_exception=True)
        new_role = ser.validated_data.role
        # Only owners may grant the OWNER role or change an owner's role —
        # otherwise any admin can promote themselves to owner. Hardcoded on
        # the `owner` role, NOT on a capability (spec §A1 invariant).
        if (new_role == Role.OWNER or member.role == Role.OWNER) and not require_role(
            workspace_id, request.user.id, Role.OWNER
        ):
            return StapelErrorResponse(403, ERR_403_FORBIDDEN_WORKSPACE)
        # Rank-gard (mandate-model vardict 2026-08-03): the capability check
        # in `_resolve` only proved "may change roles at all", not "up to
        # which rank" — see `_rank_check`. The owner-only gate above already
        # covers every OWNER-involving case (an owner's own rank never
        # exceeds itself), so this only bites the general case: a role
        # below admin that also carries `members.role.change` handing out a
        # rank above its own holder's.
        actor_membership = get_membership(workspace_id, request.user.id)
        err = _rank_check(actor_membership.role, new_role) if actor_membership else None
        if err:
            return err
        # The last-owner invariant is NOT decided here: `services.
        # change_member_role` re-reads it under the workspace lock, because
        # "another owner exists" stops being true the moment a concurrent
        # demotion commits (WORK-02).
        try:
            member = services.change_member_role(
                member=member, new_role=new_role, actor=request.user
            )
        except services.LastOwnerError:
            return StapelErrorResponse(403, ERR_403_LAST_OWNER)
        except WorkspaceMember.DoesNotExist:
            return StapelErrorResponse(404, ERR_404_MEMBER_NOT_FOUND)
        # Other services cache membership lookups — drop the stale role.
        invalidate_membership_cache(workspace_id, user_id)
        workspace_member_changed.send(
            sender=WorkspaceMember,
            workspace=member.workspace,
            user=member.user,
            role=member.role,
            action="updated",
        )
        return StapelResponse(
            self.get_response_serializer_class()(
                _member_to_dto(
                    member, _member_display_names([member]).get(str(member.user_id))
                )
            )
        )

    @extend_schema(responses={204: None})
    def delete(self, request, workspace_id, user_id):  # noqa: R007
        member, err = self._resolve(request, workspace_id, user_id, "members.remove")
        if err:
            return err
        # Only owners may remove an owner.
        if member.role == Role.OWNER and not require_role(
            workspace_id, request.user.id, Role.OWNER
        ):
            return StapelErrorResponse(403, ERR_403_FORBIDDEN_WORKSPACE)
        # Same as the demotion above: the surviving-owner question is
        # answered inside the workspace lock, not from this snapshot.
        try:
            workspace, removed_user, removed_role = services.remove_member(
                member=member, actor=request.user
            )
        except services.LastOwnerError:
            return StapelErrorResponse(403, ERR_403_LAST_OWNER)
        except WorkspaceMember.DoesNotExist:
            return StapelErrorResponse(404, ERR_404_MEMBER_NOT_FOUND)
        # Other services cache membership lookups — drop the stale entry.
        invalidate_membership_cache(workspace_id, user_id)
        workspace_member_changed.send(
            sender=WorkspaceMember,
            workspace=workspace,
            user=removed_user,
            role=removed_role,
            action="removed",
        )
        return StapelResponse(status=status.HTTP_204_NO_CONTENT)


@extend_schema(tags=["Members"])
class MemberPasswordResetView(SerializerSeamsMixin, APIView):
    """Reset a member's password on the organization's order (#110).

    Five questions this endpoint has to answer out loud, because a
    password reset is an account takeover performed on purpose and every
    one of them is a way to get it wrong.

    **Who may do it.** A mandate, not a session: capability
    ``members.password.reset`` (builtin ``admin`` and ``owner``), declared
    ``high``, so ``@requires_verification(scope="sensitive")`` demands a
    fresh step-up on top — an ambient cookie is not enough to hand
    somebody else's account over. Only an owner may reset an OWNER's
    password, the same hardcoded owner protection role changes and
    removals carry: otherwise an admin resets the owner and inherits the
    organization. And auth refuses a staff/superuser target outright
    (``error.403.privileged_account``) — org admin is a role inside one
    workspace, deployment staff is a role above every workspace, and the
    first must never be a route to the second.

    **Whether the user finds out.** Always. A
    ``workspace.member_password_reset`` letter names the workspace and the
    admin who did it — a reset is indistinguishable from a takeover
    unless the account holder is told which one it was. ``notified`` in
    the response says honestly whether a channel existed; the letter
    never carries the new password (see
    :func:`~stapel_workspaces.services.reset_member_password`).

    **Whether the new password is temporary.** Yes — auth raises the
    workspace's ``provisioned_user_policies`` (#90), defaulting to
    ``password_change``. A password the admin knows must stop working the
    first time it is used, and since auth 0.15.0 that demand holds on all
    19 session-issuance paths rather than only the password form. An org
    may pass an explicit ``[]`` to suppress it, and that lands in auth's
    audit row.

    **Whether it is an existence oracle.** No: a target that is not a
    resettable member of THIS workspace — an unknown UUID, a real account
    that is not a member, a member of a different workspace, or the
    caller's own id — all get one byte-identical 404. And the capability
    check runs before any target lookup, so a caller without the mandate
    learns nothing at all about anybody. ``tests/
    test_api_member_password_reset.py`` compares those responses byte for
    byte.

    **Whether it is logged with the actor.** Twice, on purpose:
    ``workspace.member_password_reset`` through the transactional outbox
    (the org's activity log) and auth's own ``AuthAuditLog`` row carrying
    ``actor_id`` and ``via=admin_reset`` (the deployment's security
    journal). Neither carries credential material.

    Own password: use auth's ``POST /password/change/``. This endpoint is
    for acting on somebody else, and answers about yourself with the same
    404 as about a stranger.
    """

    permission_classes = [permissions.IsAuthenticated]
    # A guest fails it twice over, like provisioning: no step-up factor to
    # satisfy `sensitive`, and no membership to carry the capability.
    stapel_anonymous_access = ANONYMOUS_DENIED
    request_serializer_class = MemberPasswordResetRequestSerializer
    response_serializer_class = MemberPasswordResetResponseSerializer

    def _resolve_target(self, request, workspace_id, user_id):
        """The workspace, the target member, or the ONE refusal shape.

        Order is the security property. The capability is checked before
        any row is read, so a caller without the mandate cannot use this
        endpoint to ask questions. Everything after that which is not a
        resettable member of this workspace collapses into a single
        ``member_not_found``.
        """
        ws = Workspace.objects.filter(id=workspace_id, deleted_at__isnull=True).first()
        if not ws:
            return None, None, StapelErrorResponse(404, ERR_404_WORKSPACE_NOT_FOUND)
        err = _capability_check(
            get_membership(ws.id, request.user.id, include_suspended=True),
            "members.password.reset",
        )
        if err:
            return None, None, err
        member = WorkspaceMember.objects.filter(
            workspace_id=ws.id, user_id=user_id
        ).first()
        # Yourself is not in the set this endpoint acts on, and says so
        # with the same answer as every other target outside it — there is
        # nothing to learn from the difference, and one shape is one shape.
        if member is None or str(member.user_id) == str(request.user.id):
            return None, None, StapelErrorResponse(404, ERR_404_MEMBER_NOT_FOUND)
        return ws, member, None

    @extend_schema(
        request=MemberPasswordResetRequestSerializer,
        responses={200: MemberPasswordResetResponseSerializer},
    )
    @requires_verification(scope="sensitive")
    def post(self, request, workspace_id, user_id):  # noqa: R007
        ws, member, err = self._resolve_target(request, workspace_id, user_id)
        if err:
            return err
        # Only an owner may reset an owner's password — the same hardcoded
        # owner protection as role changes and removals. Not folded into
        # the 404: who owns the workspace is on the roster this caller can
        # already read, so nothing leaks, and an admin who tried deserves
        # to know why it was refused.
        if member.role == Role.OWNER and not require_role(
            ws.id, request.user.id, Role.OWNER
        ):
            return StapelErrorResponse(403, ERR_403_FORBIDDEN_WORKSPACE)
        ser = self.get_request_serializer_class()(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data
        try:
            generated, revoked, applied, notified = services.reset_member_password(
                workspace=ws,
                member=member,
                reset_by=request.user,
                password=getattr(data, "password", None),
                first_login_policies=getattr(data, "first_login_policies", None),
                reason=getattr(data, "reason", None),
            )
        except services.ProvisionError as failure:
            # Auth's structured refusal, passed through keyed — the status
            # is encoded in the key (error.403.privileged_account,
            # error.404.not_found, error.400.bad_request).
            return StapelErrorResponse(
                _status_of_error_key(failure.error_key), failure.error_key
            )
        except (FunctionNotRegistered, FunctionRouteNotConfigured):
            return StapelErrorResponse(503, ERR_503_AUTH_UNAVAILABLE)
        return StapelResponse(
            self.get_response_serializer_class()(
                MemberPasswordResetResponse(
                    user_id=member.user_id,
                    generated_password=generated,
                    sessions_revoked=revoked,
                    first_login_policies_applied=applied,
                    notified=notified,
                )
            )
        )


#: "May this actor manage this workspace's PEOPLE" — the capability the
#: role-change PATCH already gates on (builtin: owner via "*", admin
#: explicitly; member/viewer do not hold it). Both name-edit endpoints share
#: it ON PURPOSE, and deliberately do not take the invitation surface's
#: `members.invite`: the member's name and the pending invitation's name
#: hint are the SAME name on either side of acceptance (the hint is copied
#: onto the membership at accept). A registry that split the two would let a
#: custom role fix a name that silently reverts the moment the person
#: accepts — a distinction the product does not draw and cannot explain.
CAPABILITY_MANAGE_PEOPLE = "members.role.change"


class DisplayNameEditMixin(SerializerSeamsMixin):
    """Shared body of the roster's two name-edit PATCHes.

    An owner/admin fixes how a person is shown to the workspace — a typo in
    the name an org admin typed at invite time, a legal-name change, a
    provisioned account created as "user-4831" — without waiting for that
    person to do it themselves. Deliberately NOT a self-service surface:
    the person's own name editor is stapel-profiles' ``PATCH /me``, and this
    one exists because a roster with wrong names on it is the org's problem,
    not only the named person's.
    """

    permission_classes = [permissions.IsAuthenticated]
    # A guest holds no WorkspaceMember row anywhere, so `_capability_check`
    # answers 403 forbidden_workspace before any row of anybody's is read.
    # The declaration does not add the gate; it makes it readable from the
    # class header (see this module's docstring).
    stapel_anonymous_access = ANONYMOUS_DENIED
    request_serializer_class = DisplayNameUpdateRequestSerializer
    response_serializer_class = DisplayNameResponseSerializer

    def _clean_name(self, request) -> str:
        """The trimmed, canon-checked name from the body.

        Raises DRF's ValidationError (→ 400) carrying either the column
        ceiling's ``error.400.field.max_length`` or one of
        stapel-profiles' own ``error.400.display_name_*`` keys — see
        ``DisplayNameUpdateRequestSerializer``.
        """
        ser = self.get_request_serializer_class()(data=request.data)
        ser.is_valid(raise_exception=True)
        return ser.validated_data.display_name or ""

    def _stored(self, display_name: str):
        return StapelResponse(
            self.get_response_serializer_class()(
                DisplayNameResponse(display_name=display_name)
            )
        )


@extend_schema(tags=["Members"])
class MemberNameView(DisplayNameEditMixin, APIView):
    """``PATCH <ws>/members/<user_id>/name`` — correct a member's display name.

    Writes the CANONICAL name: stapel-profiles' ``Profile.display_name``,
    through the named write that module publishes
    (``profiles.set_display_name``, called by
    ``services.set_profile_display_name``), which validates against its own
    canon and publishes ``profile.changed`` so every consumer of that name
    follows. Topology-independent: the same call runs in-process in a
    monolith and over the configured route where profiles is its own
    container. Until 0.21.0 it was dotted-path symbol resolution instead,
    and this endpoint simply did not work in a split deployment.

    NOT ``WorkspaceMember.display_name_hint``: the hint is a pre-profile
    placeholder, copied once at creation and dark from the moment a real
    profile exists (see its docstring in ``models.py``). Writing it would
    produce a correction the roster shows and nothing else in the product
    ever does — including, eventually, the roster.

    Where the write cannot be performed the answer is a 503 that names its
    own cause — ``error.503.profiles_not_configured`` when this deployment
    has neither a provider nor a route (an operator's job, and it will not
    heal on its own), ``error.503.profiles_unavailable`` when the call was
    made and failed — never a 200 over a write that did not happen.

    Only an owner may rename an owner — the same hardcoded owner protection
    that role changes, removals and password resets carry. Renaming is not
    escalation, but an admin relabelling the owner of the organization on
    every screen in the product is close enough to the same act to answer
    the same way.
    """

    @extend_schema(
        request=DisplayNameUpdateRequestSerializer,
        responses={200: DisplayNameResponseSerializer},
    )
    def patch(self, request, workspace_id, user_id):  # noqa: R007
        err = _capability_check(
            get_membership(workspace_id, request.user.id, include_suspended=True),
            CAPABILITY_MANAGE_PEOPLE,
        )
        if err:
            return err
        # Scoped to THIS workspace: an admin of another org holds no
        # capability here and never reaches this line, and a user who is a
        # member somewhere else is simply not in this set — one 404, the
        # same one an unknown UUID gets.
        member = WorkspaceMember.objects.filter(
            workspace_id=workspace_id, user_id=user_id
        ).first()
        if member is None:
            return StapelErrorResponse(404, ERR_404_MEMBER_NOT_FOUND)
        if member.role == Role.OWNER and not require_role(
            workspace_id, request.user.id, Role.OWNER
        ):
            return StapelErrorResponse(403, ERR_403_FORBIDDEN_WORKSPACE)
        display_name = self._clean_name(request)
        # One structural result in, one keyed refusal out: profiles' own
        # error.400.display_name_* keys pass through verbatim, and the two
        # 503s are told apart (configuration vs outage) rather than being
        # collapsed into one hint that tells an operator's problem to wait.
        error_key = services.set_profile_display_name(member.user_id, display_name)
        if error_key is not None:
            return StapelErrorResponse(_status_of_error_key(error_key), error_key)
        return self._stored(display_name)


@extend_schema(tags=["Members"])
class InvitationNameView(DisplayNameEditMixin, WorkspaceInvitationActionView):
    """``PATCH <ws>/invitations/<id>/name`` — fix a pending invite's name hint.

    The same correction as :class:`MemberNameView`, one step earlier: the
    invitee has not accepted, so there is no profile of theirs to write and
    the name lives on the invitation as ``display_name_hint`` — the invite
    modal's "Name" field, which ``accept_invitation`` copies onto the
    membership at acceptance. Editing it after the fact is why this endpoint
    exists: before #109's invitation surface the only fix for a typo in an
    invitee's name was to revoke and re-invite, which re-mails the person.

    Only a **pending** invitation is editable, with the same keyed refusals
    revoke gives for each terminal state (``invitation_revoked`` /
    ``already_used`` / ``declined`` / ``expired``): an accepted invitation's
    name is the member's name now — use the member endpoint — and a dead
    invitation is not a thing to relabel. Unknown and cross-workspace ids
    collapse into one identical 404 (inherited resolution).

    The value is still held to stapel-profiles' name canon even though the
    column being written is local: this hint becomes a displayed name, and
    the two endpoints must not disagree about what a name may contain.
    """

    capability = CAPABILITY_MANAGE_PEOPLE
    request_serializer_class = DisplayNameUpdateRequestSerializer
    response_serializer_class = DisplayNameResponseSerializer

    @extend_schema(
        request=DisplayNameUpdateRequestSerializer,
        responses={200: DisplayNameResponseSerializer},
    )
    def patch(self, request, workspace_id, invitation_id):  # noqa: R007
        _ws, inv, err = self._resolve(request, workspace_id, invitation_id)
        if err:
            return err
        err = _invitation_state_error(inv)
        if err:
            return err
        display_name = self._clean_name(request)
        # `display_name_hint` is blank=True with default="" and no null=True
        # — a cleared hint is stored as "", never None, mirroring what
        # `create_invitation` stores for an invite sent without a name.
        inv.display_name_hint = display_name
        inv.save(update_fields=["display_name_hint"])
        return self._stored(display_name)


@extend_schema(tags=["Workspaces"])
class RoleListView(SerializerSeamsMixin, APIView):
    """The effective role registry — metadata for frontends (spec §A2).

    Lets a RoleSelect stop hardcoding the builtin four: builtin roles plus
    the deployment's ``STAPEL_WORKSPACES["ROLES"]`` overlay, capability
    strings verbatim (wildcards included), ordered by descending rank.
    """

    permission_classes = [permissions.IsAuthenticated]
    # Deployment metadata, not anybody's data: the same role registry for
    # every caller, derived from BUILTIN_ROLES + STAPEL_WORKSPACES["ROLES"].
    # A guest reading it learns nothing a registered user could not, and
    # closing it would protect nothing while risking a frontend that fetches
    # the registry before it knows who is looking.
    stapel_anonymous_access = ANONYMOUS_ALLOWED
    response_serializer_class = RoleListResponseSerializer

    @extend_schema(responses={200: RoleListResponseSerializer})
    def get(self, request):  # noqa: R007
        roles = [
            RoleResponse(
                role=name,
                rank=entry.get("rank"),
                capabilities=list(entry.get("capabilities", [])),
                builtin=name in BUILTIN_ROLES,
            )
            for name, entry in effective_roles().items()
        ]
        roles.sort(key=lambda r: (-r.rank, r.role))
        return StapelResponse(
            self.get_response_serializer_class()(
                RoleListResponse(
                    roles=roles, capability_levels=effective_capability_levels()
                )
            )
        )


@extend_schema(tags=["Members"])
class InvitationAcceptView(SerializerSeamsMixin, APIView):
    permission_classes = [permissions.IsAuthenticated]
    # Gated in the body by the email match: an invitation is personal, and the
    # caller's address must equal the invited one. An anonymous account has no
    # email at all (verifying one is exactly the act that flips
    # `is_anonymous` off), so it can never match — a guest gets the same 404
    # any other wrong account gets, which is also the answer that leaks least.
    stapel_anonymous_access = ANONYMOUS_DENIED
    request_serializer_class = InvitationAcceptRequestSerializer
    response_serializer_class = MemberResponseSerializer

    @extend_schema(
        request=InvitationAcceptRequestSerializer,
        responses={200: MemberResponseSerializer},
    )
    def post(self, request):  # noqa: R007
        ser = self.get_request_serializer_class()(data=request.data)
        ser.is_valid(raise_exception=True)
        token = ser.validated_data.token
        inv = WorkspaceInvitation.objects.filter(token=token).first()
        if not inv:
            return StapelErrorResponse(404, ERR_404_INVITATION_NOT_FOUND)
        err = _invitation_state_error(inv)
        if err:
            return err
        # Invitations are personal: any token holder must not be able to
        # join with the invited role under a different account.
        if (request.user.email or "").lower() != inv.email.lower():
            return StapelErrorResponse(404, ERR_404_INVITATION_NOT_FOUND)
        if inv.workspace.deleted_at:
            return StapelErrorResponse(404, ERR_404_INVITATION_NOT_FOUND)
        try:
            member = services.accept_invitation(invitation=inv, user=request.user)
        except entitlements.EntitlementDenied as denied:
            # Entitlement seam (spec §D2): the plan ceiling is re-checked on
            # accept — the org's plan may have changed since the invite.
            return StapelErrorResponse(
                402,
                ERR_402_MEMBER_LIMIT_REACHED,
                params={
                    "limit": denied.result.limit
                    if denied.result.limit is not None
                    else 0
                },
            )
        except services.ProvisionError as failure:
            # The org configured first-login policies (#90) and auth
            # refused to raise them structurally. The membership rolled
            # back with the transaction: an org that demands a step before
            # admission does not get a member who skipped it.
            return StapelErrorResponse(
                _status_of_error_key(failure.error_key), failure.error_key
            )
        except (FunctionNotRegistered, FunctionRouteNotConfigured):
            # Same seam, wiring half: auth is not reachable, so the
            # configured precondition cannot be applied. Honest 503 — this
            # seam never degrades to allow. Only orgs that configured
            # policies can reach here; everyone else never calls auth on
            # this path at all.
            return StapelErrorResponse(503, ERR_503_AUTH_UNAVAILABLE)
        except ValueError:
            return StapelErrorResponse(400, ERR_400_INVITATION_ALREADY_USED)
        return StapelResponse(
            self.get_response_serializer_class()(
                _member_to_dto(
                    member, _member_display_names([member]).get(str(member.user_id))
                )
            )
        )


@extend_schema(tags=["Members"])
class InvitationPreviewView(TokenPathNoLogMixin, SerializerSeamsMixin, APIView):
    """Public invitation preview — what the /invite/{token} page renders.

    AllowAny by design (org-program spec §B2): the invitee has no session
    yet; the token in the URL is the bearer secret. The response leaks
    nothing harvestable — the email is masked, and ``status`` /
    ``email_registered`` are exactly what the frontend flow machine needs
    to route (login vs claim vs terminal-state screen). Throttled as an
    enumeration backstop. The token is never logged.
    """

    permission_classes = [permissions.AllowAny]
    throttle_classes = [InvitationThrottle]
    throttle_scope = "workspace-invitation"
    response_serializer_class = InvitationPreviewResponseSerializer

    @extend_schema(responses={200: InvitationPreviewResponseSerializer})
    def get(self, request, token):  # noqa: R007
        inv = (
            WorkspaceInvitation.objects.select_related("workspace")
            .filter(token=token)
            .first()
        )
        if not inv or inv.workspace.deleted_at:
            return StapelErrorResponse(404, ERR_404_INVITATION_NOT_FOUND)
        return StapelResponse(
            self.get_response_serializer_class()(
                InvitationPreviewResponse(
                    workspace_name=inv.workspace.name,
                    role=inv.role,
                    email_masked=_mask_email(inv.email),
                    status=inv.status,
                    email_registered=_email_registered(inv.email),
                    expires_at=inv.expires_at.isoformat(),
                )
            )
        )


@extend_schema(tags=["Members"])
class InvitationDeclineView(TokenPathNoLogMixin, SerializerSeamsMixin, APIView):
    """Decline an invitation — the invitee's terminal "no" (spec §B2).

    Authenticated + email-match, exactly like accept: only the invited
    account may resolve the invitation, in either direction. Decline ≠
    revoke — both states stay distinguishable in the preview ``status``.
    """

    permission_classes = [permissions.IsAuthenticated]
    # Mirror of InvitationAcceptView: the same email match keeps an anonymous
    # (email-less) session out, in the same 404 shape.
    stapel_anonymous_access = ANONYMOUS_DENIED

    @extend_schema(request=None, responses={204: None})
    def post(self, request, token):  # noqa: R007
        inv = (
            WorkspaceInvitation.objects.select_related("workspace")
            .filter(token=token)
            .first()
        )
        if not inv:
            return StapelErrorResponse(404, ERR_404_INVITATION_NOT_FOUND)
        err = _invitation_state_error(inv)
        if err:
            return err
        # Invitations are personal: any token holder must not be able to
        # resolve the invitation under a different account.
        if (request.user.email or "").lower() != inv.email.lower():
            return StapelErrorResponse(404, ERR_404_INVITATION_NOT_FOUND)
        if inv.workspace.deleted_at:
            return StapelErrorResponse(404, ERR_404_INVITATION_NOT_FOUND)
        try:
            services.decline_invitation(invitation=inv, user=request.user)
        except ValueError:
            # Raced its own state transition between the checks and the
            # locked update — the token was consumed either way.
            return StapelErrorResponse(400, ERR_400_INVITATION_ALREADY_USED)
        return StapelResponse(status=status.HTTP_204_NO_CONTENT)


@extend_schema(tags=["Members"])
class InvitationClaimView(TokenPathNoLogMixin, SerializerSeamsMixin, APIView):
    """Mint a login grant for a not-yet-registered invitee (spec §B2-B3).

    AllowAny — the whole point is that no account exists yet. Only valid
    for ``email_registered == false``: an existing account gets 409 and the
    frontend switches to login. The grant comes from auth's
    ``auth.issue_login_grant`` comm Function (``create_if_missing`` — the
    verified account materializes on exchange); if that Function is not
    wired, the answer is an honest 503 — an invite flow without auth is
    meaningless, so this seam never degrades to allow. The invitation is
    NOT consumed here: accept stays a separate, deliberate step after
    setup. Neither the invite token nor the grant token is ever logged.

    ONE live grant at a time (WORK-03): while the previous grant is inside
    its TTL this answers 429 ``error.429.invitation_grant_pending`` with a
    ``Retry-After``, so a leaked invite link cannot be replayed into an
    endless supply of sign-in credentials for the invited mailbox.
    """

    permission_classes = [permissions.AllowAny]
    throttle_classes = [InvitationThrottle]
    throttle_scope = "workspace-invitation"
    response_serializer_class = InvitationClaimResponseSerializer

    @extend_schema(request=None, responses={200: InvitationClaimResponseSerializer})
    def post(self, request, token):  # noqa: R007
        inv = (
            WorkspaceInvitation.objects.select_related("workspace")
            .filter(token=token)
            .first()
        )
        if not inv or inv.workspace.deleted_at:
            return StapelErrorResponse(404, ERR_404_INVITATION_NOT_FOUND)
        err = _invitation_state_error(inv)
        if err:
            return err
        if _email_registered(inv.email):
            return StapelErrorResponse(409, ERR_409_EMAIL_ALREADY_REGISTERED)
        language = parse_accept_language(
            request.META.get("HTTP_ACCEPT_LANGUAGE", "")
        )
        try:
            grant_token = services.issue_invitation_login_grant(
                invitation=inv, language=language
            )
        except services.LoginGrantAlreadyIssued as live:
            # One live grant per invitation (WORK-03): the link already
            # minted is still valid, and the invitee is told when they may
            # ask for another.
            resp = StapelErrorResponse(
                429,
                ERR_429_INVITATION_GRANT_PENDING,
                params={"retry_after": live.retry_after},
            )
            resp["Retry-After"] = str(live.retry_after)
            return resp
        except (FunctionNotRegistered, FunctionRouteNotConfigured):
            return StapelErrorResponse(503, ERR_503_AUTH_UNAVAILABLE)
        return StapelResponse(
            self.get_response_serializer_class()(
                InvitationClaimResponse(grant_token=grant_token)
            )
        )


@extend_schema(tags=["Internal"])
class InternalMembershipView(SerializerSeamsMixin, APIView):
    """Allow other services to check membership/role via X-API-KEY."""

    permission_classes = [IsServiceRequest | IsStaffUser]
    response_serializer_class = MemberResponseSerializer

    @extend_schema(responses={200: MemberResponseSerializer})
    def get(self, request, workspace_id, user_id):  # noqa: R007
        # Only accepted, non-suspended memberships count — a suspended
        # member must read as not-a-member to authorization consumers
        # (suspension closes access to the org entirely, spec §C3) — and
        # the same is true of a member whose MFA the workspace requires and
        # nobody has confirmed, which is why this goes through the
        # admission seam rather than round it (WORK-01).
        member = get_membership(workspace_id, user_id)
        if not member:
            return StapelErrorResponse(404, ERR_404_MEMBER_NOT_FOUND)
        return StapelResponse(
            self.get_response_serializer_class()(
                _member_to_dto(
                    member, _member_display_names([member]).get(str(member.user_id))
                )
            )
        )


@extend_schema(tags=["Internal"])
class InternalPersonalWorkspaceView(APIView):
    """Get-or-create the personal workspace for a given user_id."""

    permission_classes = [IsServiceRequest | IsStaffUser]

    @extend_schema(
        request=None,
        responses={
            200: InternalPersonalWorkspaceResponseSerializer,
            404: StapelErrorSerializer,
        },
    )
    def post(self, request, user_id):  # noqa: R007
        from django.contrib.auth import get_user_model

        User = get_user_model()

        try:
            user = User.objects.get(pk=user_id)
        except User.DoesNotExist:
            return StapelErrorResponse(404, ERR_404_WORKSPACE_NOT_FOUND)
        ws = services.ensure_personal_workspace(user)
        return StapelResponse({"workspace_id": str(ws.id)}, status=status.HTTP_200_OK)  # noqa: R006


class InstanceShapeView(APIView):
    """How this instance is deployed — public, before any authorization.

    The ``STREET_LANDING_MODE`` axis has existed since 2026-08-03 but lived
    only in backend config, exposing nothing — a client couldn't tell a
    closed deployment from a public cloud one, and rendered the same screen
    to someone with a workspace and someone who can never have one.

    Deliberately open to anonymous callers, no exceptions: this response is
    read by exactly the person who is no longer anyone to the workspace —
    kicked out or left on their own. Requiring auth here would lock out its
    only audience. Nothing secret in the response: it's the deployment
    shape, already visible from whether the instance allows registration.
    """

    permission_classes = [permissions.AllowAny]
    authentication_classes = []

    @extend_schema(
        tags=["Workspaces"],
        description=(
            "Instance shape: how a street signup lands (personal | none) and "
            "whether self-serve registration is open. Public, unauthenticated "
            "— the screen a kicked/left member sees depends on it."
        ),
        responses={200: InstanceShapeResponseSerializer},
    )
    def get(self, request):  # noqa: R007
        from .dto import InstanceShapeResponse

        from .conf import workspaces_settings

        landing = workspaces_settings.STREET_LANDING_MODE or "personal"
        return StapelResponse(
            InstanceShapeResponseSerializer(
                InstanceShapeResponse(
                    landing=landing,
                    # A closed deployment has no self-serve signup — entry is
                    # by invitation only. Same decision, named from both
                    # sides; the client needs both fields.
                    registration_open=(landing != "none"),
                )
            )
        )


class AuditPagination(InvitationPagination):
    """Anchor pagination for the audit list.

    Same shape and the same reason as the invitation list: an admin paging
    through history while new lines are still being appended must not have
    rows slip under an offset window.
    """


@extend_schema(tags=["Members"])
class WorkspaceAuditView(SerializerSeamsMixin, APIView):
    """``GET <workspace_id>/audit`` — the workspace's membership history.

    THE QUESTION THIS ANSWERS is "who let this person in, who took them out,
    and when" — which nothing in this module could answer before. Half the
    transitions the owner listed emit no comm event at all (an invitation
    created, an invitation accepted, an account born from one), and the ones
    that do emit are fire-and-forget notifications to other services: nothing
    keeps them, so there was no record to ask.

    GATED ON ``members.view``, not on a new capability of its own. An audit of
    who is in the workspace is the same class of fact as the member list —
    every role that may see who is in the room may see how they got there. A
    separate mandate would mean a deployment could grant one without the
    other, which describes no real product.

    Read-only by construction: this view has no write method, and the model
    has no update or delete path.
    """

    permission_classes = [permissions.IsAuthenticated]
    # A guest holds no membership, so `_capability_check` answers the same
    # keyed 403 as everywhere else in this module.
    stapel_anonymous_access = ANONYMOUS_DENIED
    # Declared, not merely used: drf-spectacular reads this to wrap the
    # response schema in the anchor-pagination envelope. Without it the
    # contract advertised a bare array while the endpoint returned
    # `{items, has_next, next_anchor}` — a generated client would have been
    # typed against a shape the server never sends.
    pagination_class = AuditPagination
    response_serializer_class = AuditEventResponseSerializer

    @extend_schema(
        parameters=[
            OpenApiParameter(
                "action",
                str,
                description=(
                    "Narrow to one action (models.AuditAction). An unknown "
                    "value matches nothing rather than being ignored — a "
                    "filter that silently does not apply is worse than an "
                    "empty page."
                ),
            ),
            OpenApiParameter(
                "user_id",
                str,
                description="Narrow to one person's history (as the SUBJECT).",
            ),
        ],
        responses={200: AuditEventResponseSerializer(many=True)},
    )
    def get(self, request, workspace_id):  # noqa: R007
        ws = Workspace.objects.filter(id=workspace_id, deleted_at__isnull=True).first()
        if not ws:
            return StapelErrorResponse(404, ERR_404_WORKSPACE_NOT_FOUND)
        membership = get_membership(ws.id, request.user.id, include_suspended=True)
        err = _capability_check(membership, "members.view")
        if err:
            return err

        # The history lives in the core event store (see audit.py), read
        # through the store's anchor adapter — which speaks this endpoint's
        # released wire contract, so the storage change is invisible on the
        # wire: same envelope, same anchors, same items.
        action = (request.query_params.get("action") or "").strip()
        subject_id = None
        empty = False
        subject = (request.query_params.get("user_id") or "").strip()
        if subject:
            try:
                subject_id = UUID(subject)
            except (TypeError, ValueError):
                # A malformed id matches nobody. Ignoring the filter would
                # hand back the WHOLE history under a request that asked for
                # one person's — the loudest possible wrong answer.
                empty = True
        limit = AuditPagination()._get_limit(request)
        try:
            page = (
                None
                if empty
                else audit.history_page(
                    ws.id,
                    action=action,
                    subject_id=subject_id,
                    anchor=(request.query_params.get("anchor") or "").strip() or None,
                    direction=request.query_params.get("direction", "next"),
                    limit=limit,
                )
            )
        except ValueError:
            # A garbage anchor gets the same treatment as the malformed user
            # filter above: match nothing. Restarting from page one would
            # silently hand back rows the caller already walked past.
            page = None

        events = page.events if page else []
        payloads = [e.payload for e in events]
        # ONE profiles call for every person named on the page, actors and
        # subjects together — the same batch shape the member list uses.
        names = services._fetch_profile_display_names(
            [p["actor_id"] for p in payloads if p.get("actor_id")]
            + [p["subject_id"] for p in payloads if p.get("subject_id")]
        )
        dtos = [
            AuditEventResponse(
                id=UUID(p["id"]),
                action=p["action"],
                actor_id=UUID(p["actor_id"]) if p.get("actor_id") else None,
                actor_display_name=names.get(p.get("actor_id"), "") if p.get("actor_id") else "",
                subject_id=UUID(p["subject_id"]) if p.get("subject_id") else None,
                subject_display_name=(
                    names.get(p.get("subject_id"), "") if p.get("subject_id") else ""
                ),
                subject_email=p.get("subject_email", ""),
                role=p.get("role", ""),
                metadata=p.get("metadata") or {},
                created_at=e.ts.isoformat(),
            )
            for e, p in zip(events, payloads)
        ]
        items = self.get_response_serializer_class()(dtos, many=True).data
        # The exact AnchorPagination envelope, hand-assembled: the paginator
        # class above still DECLARES the schema, the adapter now produces the
        # values (its flags follow the same contract).
        return Response(
            {
                "items": items,
                "next_anchor": page.next_anchor if page else None,
                "prev_anchor": page.prev_anchor if page else None,
                "has_next": page.has_next if page else False,
                "has_prev": page.has_prev if page else False,
                "count": len(items),
            }
        )
