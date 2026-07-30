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

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import CharField, F, Q, Value
from django.db.models.functions import Coalesce, Concat, NullIf, Trim
from django.utils import timezone
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import permissions, status
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from stapel_core.comm import emit
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

from . import entitlements, services
from .capabilities import (
    BUILTIN_ROLES,
    capabilities_for,
    effective_roles,
    role_has_capability,
)
from .dto import (
    InvitationClaimResponse,
    InvitationPreviewResponse,
    InvitationResponse,
    MemberInviteResponse,
    MemberResponse,
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
    ERR_404_INVITATION_NOT_FOUND,
    ERR_404_MEMBER_NOT_FOUND,
    ERR_404_WORKSPACE_NOT_FOUND,
    ERR_409_EMAIL_ALREADY_REGISTERED,
    ERR_503_AUTH_UNAVAILABLE,
)
from .events import (
    EVENT_WORKSPACE_MEMBER_REMOVED,
    EVENT_WORKSPACE_MEMBER_ROLE_CHANGED,
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
    InternalPersonalWorkspaceResponseSerializer,
    InvitationAcceptRequestSerializer,
    InvitationClaimResponseSerializer,
    InvitationPreviewResponseSerializer,
    InvitationResponseSerializer,
    MemberInviteRequestSerializer,
    MemberInviteResponseSerializer,
    MemberResponseSerializer,
    MemberUpdateRequestSerializer,
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


def _workspace_to_dto(
    ws: Workspace, my_role: str | None = None, member_count: int | None = None
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
    )


def _member_to_dto(m: WorkspaceMember) -> MemberResponse:
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
    )


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
    )


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
        workspaces = []
        for m in memberships:
            ws = m.workspace
            if ws.deleted_at:
                continue
            workspaces.append(_workspace_to_dto(ws, my_role=m.role))
        return StapelResponse(
            self.get_list_response_serializer_class()(
                WorkspaceListResponse(workspaces=workspaces)
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
                _workspace_to_dto(ws, my_role=membership.role)
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

        Flipping ``require_mfa`` ON triggers the synchronous member sweep
        (``auth.mfa_status`` per member; no strong factor → suspension with
        reason ``no_mfa``); auth being unavailable aborts the sweep without
        touching anyone (fail-open by suspension — spec §C3), the policy
        itself still saves and the mfa-event consumer catches up. Flipping
        it OFF lifts the ``no_mfa`` suspensions it caused.
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
        return response

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
        items = [response_cls(_member_to_dto(m)).data for m in page]
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
        err = _capability_check(
            get_membership(ws.id, request.user.id, include_suspended=True),
            "members.invite",
        )
        if err:
            return err
        ser = self.get_request_serializer_class()(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data
        # Entitlement seam (spec §D2): capability first ("may YOU", 403),
        # then the org's plan ceiling ("may the ORG", 402). Seats = accepted
        # + pending live invitations + the invitations about to be created.
        verdict = entitlements.check_org_entitlement(
            ws,
            entitlements.ENT_MEMBERS_MAX,
            quantity=entitlements.member_seats_quantity(
                ws, additional=len(data.emails)
            ),
        )
        if not verdict.allowed:
            return StapelErrorResponse(
                402,
                ERR_402_MEMBER_LIMIT_REACHED,
                params={"limit": verdict.limit if verdict.limit is not None else 0},
            )
        invitations = [
            services.create_invitation(
                workspace=ws, email=e, role=data.role, invited_by=request.user
            )
            for e in data.emails
        ]
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
    # `members.invite` capability check in `_resolve`; a guest has no
    # membership, so 403 before any invitation row is read.
    stapel_anonymous_access = ANONYMOUS_DENIED
    response_serializer_class = InvitationResponseSerializer

    def _resolve(self, request, workspace_id, invitation_id):
        ws = Workspace.objects.filter(id=workspace_id, deleted_at__isnull=True).first()
        if not ws:
            return None, None, StapelErrorResponse(404, ERR_404_WORKSPACE_NOT_FOUND)
        err = _capability_check(
            get_membership(ws.id, request.user.id, include_suspended=True),
            "members.invite",
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
    which are decisions rather than delivery failures. The token is
    rotated and the TTL restarts, so the fresh letter carries a fresh link
    and any stale copy of the old one stops working.

    Reviving an expired invitation re-reserves a seat, so the plan ceiling
    is re-checked here exactly as it is on invite: capability first ("may
    YOU", 403), then the org's plan ("may the ORG", 402). An invitation
    that is already pending costs no additional seat and is never blocked
    by that check.
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
        try:
            inv = services.resend_invitation(invitation=inv)
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
        err = _capability_check(
            get_membership(ws.id, request.user.id, include_suspended=True),
            "members.provision",
        )
        if err:
            return err
        ser = self.get_request_serializer_class()(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data
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
        if member.role == Role.OWNER and new_role != Role.OWNER:
            others = (
                WorkspaceMember.objects.filter(
                    workspace_id=workspace_id, role=Role.OWNER
                )
                .exclude(id=member.id)
                .exists()
            )
            if not others:
                return StapelErrorResponse(403, ERR_403_LAST_OWNER)
        old_role = member.role
        member.role = new_role
        with transaction.atomic():
            member.save(update_fields=["role"])
            # Transactional outbox: leaves iff this transaction commits.
            # Cross-service consumers (e.g. a rooms service re-evaluating a
            # participant's rights) get the new role's capability grants
            # inline (spec §A4).
            emit(
                EVENT_WORKSPACE_MEMBER_ROLE_CHANGED,
                {
                    "workspace_id": str(member.workspace_id),
                    "user_id": str(member.user_id),
                    "old_role": str(old_role),
                    "new_role": str(member.role),
                    "capabilities": capabilities_for(member.role),
                },
            )
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
            self.get_response_serializer_class()(_member_to_dto(member))
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
        if member.role == Role.OWNER:
            others = (
                WorkspaceMember.objects.filter(
                    workspace_id=workspace_id, role=Role.OWNER
                )
                .exclude(id=member.id)
                .exists()
            )
            if not others:
                return StapelErrorResponse(403, ERR_403_LAST_OWNER)
        workspace = member.workspace
        removed_user = member.user
        removed_role = member.role
        with transaction.atomic():
            member.delete()
            # Transactional outbox: leaves iff this transaction commits.
            # The cross-service kick signal (spec §A4) — e.g. a rooms
            # service disconnects the user from an ongoing call.
            emit(
                EVENT_WORKSPACE_MEMBER_REMOVED,
                {
                    "workspace_id": str(workspace.id),
                    "user_id": str(removed_user.pk),
                    "role": str(removed_role),
                    "removed_by": str(request.user.pk),
                },
            )
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
            self.get_response_serializer_class()(RoleListResponse(roles=roles))
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
        except ValueError:
            return StapelErrorResponse(400, ERR_400_INVITATION_ALREADY_USED)
        return StapelResponse(
            self.get_response_serializer_class()(_member_to_dto(member))
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
        # (suspension closes access to the org entirely, spec §C3).
        member = (
            WorkspaceMember.objects.active()
            .filter(workspace_id=workspace_id, user_id=user_id)
            .select_related("user")
            .first()
        )
        if not member:
            return StapelErrorResponse(404, ERR_404_MEMBER_NOT_FOUND)
        return StapelResponse(
            self.get_response_serializer_class()(_member_to_dto(member))
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
