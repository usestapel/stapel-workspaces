"""DRF views for the workspaces service."""

from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import permissions, status
from rest_framework.views import APIView
from stapel_core.django.api.errors import StapelErrorResponse, StapelResponse
from stapel_core.django.api.permissions import IsServiceRequest, IsStaffUser
from stapel_core.django.workspaces import invalidate_membership_cache
from stapel_core.signals import workspace_member_changed

from . import services
from .dto import (
    InvitationResponse,
    MemberInviteResponse,
    MemberListResponse,
    MemberResponse,
    WorkspaceListResponse,
    WorkspaceResponse,
)
from .errors import (
    ERR_400_INVITATION_ALREADY_USED,
    ERR_400_INVITATION_EXPIRED,
    ERR_400_INVITATION_REVOKED,
    ERR_400_SLUG_TAKEN,
    ERR_403_FORBIDDEN_WORKSPACE,
    ERR_403_LAST_OWNER,
    ERR_404_INVITATION_NOT_FOUND,
    ERR_404_MEMBER_NOT_FOUND,
    ERR_404_WORKSPACE_NOT_FOUND,
)
from .models import Role, Workspace, WorkspaceInvitation, WorkspaceMember
from .permissions import require_role, role_at_least
from .serializers import (
    InvitationAcceptRequestSerializer,
    MemberInviteRequestSerializer,
    MemberInviteResponseSerializer,
    MemberListResponseSerializer,
    MemberResponseSerializer,
    MemberUpdateRequestSerializer,
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
    def get(self, request):
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
    def post(self, request):
        ser = self.get_request_serializer_class()(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data
        slug = getattr(data, "slug", None)
        if slug and Workspace.objects.filter(slug=slug).exists():
            return StapelErrorResponse(400, ERR_400_SLUG_TAKEN)
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

    def _resolve(self, request, workspace_id):
        ws = Workspace.objects.filter(id=workspace_id, deleted_at__isnull=True).first()
        if not ws:
            return None, None, StapelErrorResponse(404, ERR_404_WORKSPACE_NOT_FOUND)
        membership = require_role(ws.id, request.user.id, Role.VIEWER)
        if not membership:
            return None, None, StapelErrorResponse(403, ERR_403_FORBIDDEN_WORKSPACE)
        return ws, membership, None

    @extend_schema(responses={200: WorkspaceResponseSerializer})
    def get(self, request, workspace_id):
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
    def patch(self, request, workspace_id):
        ws, membership, err = self._resolve(request, workspace_id)
        if err:
            return err
        if not role_at_least(membership.role, Role.ADMIN):
            return StapelErrorResponse(403, ERR_403_FORBIDDEN_WORKSPACE)
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
    def delete(self, request, workspace_id):
        ws, membership, err = self._resolve(request, workspace_id)
        if err:
            return err
        if not role_at_least(membership.role, Role.OWNER):
            return StapelErrorResponse(403, ERR_403_FORBIDDEN_WORKSPACE)
        ws.deleted_at = timezone.now()
        ws.save(update_fields=["deleted_at"])
        return StapelResponse(status=status.HTTP_204_NO_CONTENT)


@extend_schema(tags=["Members"])
class MemberListView(SerializerSeamsMixin, APIView):
    permission_classes = [permissions.IsAuthenticated]
    response_serializer_class = MemberListResponseSerializer

    @extend_schema(responses={200: MemberListResponseSerializer})
    def get(self, request, workspace_id):
        if not require_role(workspace_id, request.user.id, Role.VIEWER):
            return StapelErrorResponse(403, ERR_403_FORBIDDEN_WORKSPACE)
        members = WorkspaceMember.objects.filter(
            workspace_id=workspace_id
        ).select_related("user")
        return StapelResponse(
            self.get_response_serializer_class()(
                MemberListResponse(members=[_member_to_dto(m) for m in members])
            )
        )


@extend_schema(tags=["Members"])
class MemberInviteView(SerializerSeamsMixin, APIView):
    permission_classes = [permissions.IsAuthenticated]
    request_serializer_class = MemberInviteRequestSerializer
    response_serializer_class = MemberInviteResponseSerializer

    @extend_schema(
        request=MemberInviteRequestSerializer,
        responses={201: MemberInviteResponseSerializer},
    )
    def post(self, request, workspace_id):
        ws = Workspace.objects.filter(id=workspace_id, deleted_at__isnull=True).first()
        if not ws:
            return StapelErrorResponse(404, ERR_404_WORKSPACE_NOT_FOUND)
        if not require_role(ws.id, request.user.id, Role.ADMIN):
            return StapelErrorResponse(403, ERR_403_FORBIDDEN_WORKSPACE)
        ser = self.get_request_serializer_class()(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data
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

    def _resolve(self, request, workspace_id, user_id):
        if not require_role(workspace_id, request.user.id, Role.ADMIN):
            return None, StapelErrorResponse(403, ERR_403_FORBIDDEN_WORKSPACE)
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
    def patch(self, request, workspace_id, user_id):
        member, err = self._resolve(request, workspace_id, user_id)
        if err:
            return err
        ser = self.get_request_serializer_class()(data=request.data)
        ser.is_valid(raise_exception=True)
        new_role = ser.validated_data.role
        # Only owners may grant the OWNER role or change an owner's role —
        # otherwise any admin can promote themselves to owner.
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
        member.role = new_role
        member.save(update_fields=["role"])
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
    def delete(self, request, workspace_id, user_id):
        member, err = self._resolve(request, workspace_id, user_id)
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
        member.delete()
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
class InvitationAcceptView(SerializerSeamsMixin, APIView):
    permission_classes = [permissions.IsAuthenticated]
    request_serializer_class = InvitationAcceptRequestSerializer
    response_serializer_class = MemberResponseSerializer

    @extend_schema(
        request=InvitationAcceptRequestSerializer,
        responses={200: MemberResponseSerializer},
    )
    def post(self, request):
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
    def get(self, request, workspace_id, user_id):
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


class InternalPersonalWorkspaceView(APIView):
    """Get-or-create the personal workspace for a given user_id."""

    permission_classes = [IsServiceRequest | IsStaffUser]

    def post(self, request, user_id):
        from django.contrib.auth import get_user_model

        User = get_user_model()

        try:
            user = User.objects.get(pk=user_id)
        except User.DoesNotExist:
            return StapelErrorResponse(404, ERR_404_WORKSPACE_NOT_FOUND)
        ws = services.ensure_personal_workspace(user)
        return StapelResponse({"workspace_id": str(ws.id)}, status=status.HTTP_200_OK)
