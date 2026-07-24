"""DRF views for the workspaces service."""

from django.db import transaction
from django.db.models import CharField, F, Q, Value
from django.db.models.functions import Coalesce, Concat, NullIf, Trim
from django.utils import timezone
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import permissions, status
from rest_framework.views import APIView
from stapel_core.comm import emit
from stapel_core.django.api.errors import StapelErrorResponse, StapelResponse
from stapel_core.django.api.pagination import AnchorPagination
from stapel_core.django.api.permissions import IsServiceRequest, IsStaffUser
from stapel_core.django.openapi.schemas import StapelErrorSerializer
from stapel_core.django.workspaces import invalidate_membership_cache
from stapel_core.signals import workspace_member_changed

from . import entitlements, services
from .capabilities import (
    BUILTIN_ROLES,
    capabilities_for,
    effective_roles,
    role_has_capability,
)
from .dto import (
    InvitationResponse,
    MemberInviteResponse,
    MemberResponse,
    RoleListResponse,
    RoleResponse,
    WorkspaceListResponse,
    WorkspaceResponse,
)
from .errors import (
    ERR_400_INVITATION_ALREADY_USED,
    ERR_400_INVITATION_EXPIRED,
    ERR_400_INVITATION_REVOKED,
    ERR_400_SLUG_TAKEN,
    ERR_402_ENTITLEMENT_REQUIRED,
    ERR_402_MEMBER_LIMIT_REACHED,
    ERR_403_FORBIDDEN_WORKSPACE,
    ERR_403_LAST_OWNER,
    ERR_403_MISSING_CAPABILITY,
    ERR_404_INVITATION_NOT_FOUND,
    ERR_404_MEMBER_NOT_FOUND,
    ERR_404_WORKSPACE_NOT_FOUND,
)
from .events import (
    EVENT_WORKSPACE_MEMBER_REMOVED,
    EVENT_WORKSPACE_MEMBER_ROLE_CHANGED,
)
from .models import Role, Workspace, WorkspaceInvitation, WorkspaceMember, WorkspaceType
from .permissions import get_membership, require_role, role_at_least
from .serializers import (
    InternalPersonalWorkspaceResponseSerializer,
    InvitationAcceptRequestSerializer,
    MemberInviteRequestSerializer,
    MemberInviteResponseSerializer,
    MemberResponseSerializer,
    MemberUpdateRequestSerializer,
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
    """403 mapping for the capability layer (org-program spec §A2).

    Not a member at all → the historical ``forbidden_workspace`` boundary;
    a member whose role lacks the capability → ``missing_capability`` with
    the capability string as a param.
    """
    if membership is None:
        return StapelErrorResponse(403, ERR_403_FORBIDDEN_WORKSPACE)
    if not role_has_capability(membership.role, capability):
        return StapelErrorResponse(
            403, ERR_403_MISSING_CAPABILITY, params={"capability": capability}
        )
    return None


def _workspace_to_dto(
    ws: Workspace, my_role: str | None = None, member_count: int | None = None
) -> WorkspaceResponse:
    if member_count is None:
        member_count = ws.members.filter(accepted_at__isnull=False).count()
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


def _invitation_to_dto(inv: WorkspaceInvitation) -> InvitationResponse:
    return InvitationResponse(
        id=inv.id,
        workspace_id=inv.workspace_id,
        email=inv.email,
        role=inv.role,
        expires_at=inv.expires_at.isoformat(),
        accepted_at=inv.accepted_at.isoformat() if inv.accepted_at else None,
        revoked_at=inv.revoked_at.isoformat() if inv.revoked_at else None,
        created_at=inv.created_at.isoformat(),
    )


@extend_schema(tags=["Workspaces"])
class WorkspaceListCreateView(SerializerSeamsMixin, APIView):
    permission_classes = [permissions.IsAuthenticated]
    request_serializer_class = WorkspaceCreateRequestSerializer
    response_serializer_class = WorkspaceResponseSerializer
    list_response_serializer_class = WorkspaceListResponseSerializer

    def get_list_response_serializer_class(self):
        return self.list_response_serializer_class

    @extend_schema(responses={200: WorkspaceListResponseSerializer})
    def get(self, request):  # noqa: R007
        memberships = (
            WorkspaceMember.objects.filter(user=request.user, accepted_at__isnull=False)
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
    request_serializer_class = WorkspaceUpdateRequestSerializer
    response_serializer_class = WorkspaceResponseSerializer

    def _resolve(self, request, workspace_id, capability: str = "workspace.view"):
        ws = Workspace.objects.filter(id=workspace_id, deleted_at__isnull=True).first()
        if not ws:
            return None, None, StapelErrorResponse(404, ERR_404_WORKSPACE_NOT_FOUND)
        membership = get_membership(ws.id, request.user.id)
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
            get_membership(workspace_id, request.user.id), "members.view"
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
            get_membership(ws.id, request.user.id), "members.invite"
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


@extend_schema(tags=["Members"])
class MemberDetailView(SerializerSeamsMixin, APIView):
    permission_classes = [permissions.IsAuthenticated]
    request_serializer_class = MemberUpdateRequestSerializer
    response_serializer_class = MemberResponseSerializer

    def _resolve(self, request, workspace_id, user_id, capability):
        err = _capability_check(
            get_membership(workspace_id, request.user.id), capability
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
        if inv.revoked_at:
            return StapelErrorResponse(400, ERR_400_INVITATION_REVOKED)
        if inv.accepted_at:
            return StapelErrorResponse(400, ERR_400_INVITATION_ALREADY_USED)
        if inv.expires_at and inv.expires_at < timezone.now():
            return StapelErrorResponse(400, ERR_400_INVITATION_EXPIRED)
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


@extend_schema(tags=["Internal"])
class InternalMembershipView(SerializerSeamsMixin, APIView):
    """Allow other services to check membership/role via X-API-KEY."""

    permission_classes = [IsServiceRequest | IsStaffUser]
    response_serializer_class = MemberResponseSerializer

    @extend_schema(responses={200: MemberResponseSerializer})
    def get(self, request, workspace_id, user_id):  # noqa: R007
        member = (
            WorkspaceMember.objects.filter(
                workspace_id=workspace_id, user_id=user_id, accepted_at__isnull=False
            )
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
