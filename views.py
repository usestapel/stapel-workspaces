"""DRF views for the workspaces service."""

from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import permissions, status
from rest_framework.views import APIView

from stapel_core.django.errors import IronResponse, IronErrorResponse
from stapel_core.django.permissions import IsServiceRequest, IsStaffUser

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


def _workspace_to_dto(ws: Workspace, my_role: str | None = None, member_count: int | None = None) -> WorkspaceResponse:
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
class WorkspaceListCreateView(APIView):
    permission_classes = [permissions.IsAuthenticated]

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
        return IronResponse(
            WorkspaceListResponseSerializer(
                WorkspaceListResponse(workspaces=workspaces)
            )
        )

    @extend_schema(
        request=WorkspaceCreateRequestSerializer,
        responses={201: WorkspaceResponseSerializer},
    )
    def post(self, request):
        ser = WorkspaceCreateRequestSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data
        slug = getattr(data, "slug", None)
        if slug and Workspace.objects.filter(slug=slug).exists():
            return IronErrorResponse(400, ERR_400_SLUG_TAKEN)
        ws = services.create_workspace(
            user=request.user,
            name=data.name,
            slug=slug,
            type=data.type or "work",
        )
        return IronResponse(
            WorkspaceResponseSerializer(_workspace_to_dto(ws, my_role=Role.OWNER)),
            status=status.HTTP_201_CREATED,
        )


@extend_schema(tags=["Workspaces"])
class WorkspaceDetailView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def _resolve(self, request, workspace_id):
        ws = Workspace.objects.filter(id=workspace_id, deleted_at__isnull=True).first()
        if not ws:
            return None, None, IronErrorResponse(404, ERR_404_WORKSPACE_NOT_FOUND)
        membership = require_role(ws.id, request.user.id, Role.VIEWER)
        if not membership:
            return None, None, IronErrorResponse(403, ERR_403_FORBIDDEN_WORKSPACE)
        return ws, membership, None

    @extend_schema(responses={200: WorkspaceResponseSerializer})
    def get(self, request, workspace_id):
        ws, membership, err = self._resolve(request, workspace_id)
        if err:
            return err
        membership.last_accessed_at = timezone.now()
        membership.save(update_fields=["last_accessed_at"])
        return IronResponse(
            WorkspaceResponseSerializer(
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
            return IronErrorResponse(403, ERR_403_FORBIDDEN_WORKSPACE)
        ser = WorkspaceUpdateRequestSerializer(data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data
        new_slug = getattr(data, "slug", None)
        if new_slug and new_slug != ws.slug:
            if Workspace.objects.filter(slug=new_slug).exclude(id=ws.id).exists():
                return IronErrorResponse(400, ERR_400_SLUG_TAKEN)
            ws.slug = new_slug
        if getattr(data, "name", None):
            ws.name = data.name
        if getattr(data, "settings", None) is not None:
            ws.settings = data.settings
        ws.save()
        return IronResponse(
            WorkspaceResponseSerializer(
                _workspace_to_dto(ws, my_role=membership.role)
            )
        )

    @extend_schema(responses={204: None})
    def delete(self, request, workspace_id):
        ws, membership, err = self._resolve(request, workspace_id)
        if err:
            return err
        if not role_at_least(membership.role, Role.OWNER):
            return IronErrorResponse(403, ERR_403_FORBIDDEN_WORKSPACE)
        ws.deleted_at = timezone.now()
        ws.save(update_fields=["deleted_at"])
        return IronResponse(status=status.HTTP_204_NO_CONTENT)


@extend_schema(tags=["Members"])
class MemberListView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(responses={200: MemberListResponseSerializer})
    def get(self, request, workspace_id):
        if not require_role(workspace_id, request.user.id, Role.VIEWER):
            return IronErrorResponse(403, ERR_403_FORBIDDEN_WORKSPACE)
        members = WorkspaceMember.objects.filter(
            workspace_id=workspace_id
        ).select_related("user")
        return IronResponse(
            MemberListResponseSerializer(
                MemberListResponse(members=[_member_to_dto(m) for m in members])
            )
        )


@extend_schema(tags=["Members"])
class MemberInviteView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(
        request=MemberInviteRequestSerializer,
        responses={201: MemberInviteResponseSerializer},
    )
    def post(self, request, workspace_id):
        ws = Workspace.objects.filter(id=workspace_id, deleted_at__isnull=True).first()
        if not ws:
            return IronErrorResponse(404, ERR_404_WORKSPACE_NOT_FOUND)
        if not require_role(ws.id, request.user.id, Role.ADMIN):
            return IronErrorResponse(403, ERR_403_FORBIDDEN_WORKSPACE)
        ser = MemberInviteRequestSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data
        invitations = [
            services.create_invitation(
                workspace=ws, email=e, role=data.role, invited_by=request.user
            )
            for e in data.emails
        ]
        return IronResponse(
            MemberInviteResponseSerializer(
                MemberInviteResponse(
                    invitations=[_invitation_to_dto(i) for i in invitations]
                )
            ),
            status=status.HTTP_201_CREATED,
        )


@extend_schema(tags=["Members"])
class MemberDetailView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def _resolve(self, request, workspace_id, user_id):
        if not require_role(workspace_id, request.user.id, Role.ADMIN):
            return None, IronErrorResponse(403, ERR_403_FORBIDDEN_WORKSPACE)
        member = WorkspaceMember.objects.filter(
            workspace_id=workspace_id, user_id=user_id
        ).first()
        if not member:
            return None, IronErrorResponse(404, ERR_404_MEMBER_NOT_FOUND)
        return member, None

    @extend_schema(
        request=MemberUpdateRequestSerializer,
        responses={200: MemberResponseSerializer},
    )
    def patch(self, request, workspace_id, user_id):
        member, err = self._resolve(request, workspace_id, user_id)
        if err:
            return err
        ser = MemberUpdateRequestSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        new_role = ser.validated_data.role
        if member.role == Role.OWNER and new_role != Role.OWNER:
            others = WorkspaceMember.objects.filter(
                workspace_id=workspace_id, role=Role.OWNER
            ).exclude(id=member.id).exists()
            if not others:
                return IronErrorResponse(403, ERR_403_LAST_OWNER)
        member.role = new_role
        member.save(update_fields=["role"])
        return IronResponse(MemberResponseSerializer(_member_to_dto(member)))

    @extend_schema(responses={204: None})
    def delete(self, request, workspace_id, user_id):
        member, err = self._resolve(request, workspace_id, user_id)
        if err:
            return err
        if member.role == Role.OWNER:
            others = WorkspaceMember.objects.filter(
                workspace_id=workspace_id, role=Role.OWNER
            ).exclude(id=member.id).exists()
            if not others:
                return IronErrorResponse(403, ERR_403_LAST_OWNER)
        member.delete()
        return IronResponse(status=status.HTTP_204_NO_CONTENT)


@extend_schema(tags=["Members"])
class InvitationAcceptView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(
        request=InvitationAcceptRequestSerializer,
        responses={200: MemberResponseSerializer},
    )
    def post(self, request):
        ser = InvitationAcceptRequestSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        token = ser.validated_data.token
        inv = WorkspaceInvitation.objects.filter(token=token).first()
        if not inv:
            return IronErrorResponse(404, ERR_404_INVITATION_NOT_FOUND)
        if inv.revoked_at:
            return IronErrorResponse(400, ERR_400_INVITATION_REVOKED)
        if inv.accepted_at:
            return IronErrorResponse(400, ERR_400_INVITATION_ALREADY_USED)
        if inv.expires_at and inv.expires_at < timezone.now():
            return IronErrorResponse(400, ERR_400_INVITATION_EXPIRED)
        member = services.accept_invitation(invitation=inv, user=request.user)
        return IronResponse(MemberResponseSerializer(_member_to_dto(member)))


@extend_schema(tags=["Internal"])
class InternalMembershipView(APIView):
    """Allow other services to check membership/role via X-API-KEY."""

    permission_classes = [IsServiceRequest | IsStaffUser]

    @extend_schema(responses={200: MemberResponseSerializer})
    def get(self, request, workspace_id, user_id):
        member = WorkspaceMember.objects.filter(
            workspace_id=workspace_id, user_id=user_id, accepted_at__isnull=False
        ).select_related("user").first()
        if not member:
            return IronErrorResponse(404, ERR_404_MEMBER_NOT_FOUND)
        return IronResponse(MemberResponseSerializer(_member_to_dto(member)))


class InternalPersonalWorkspaceView(APIView):
    """Get-or-create the personal workspace for a given user_id."""

    permission_classes = [IsServiceRequest | IsStaffUser]

    def post(self, request, user_id):
        from stapel_core.django.users.models import User
        try:
            user = User.objects.get(pk=user_id)
        except User.DoesNotExist:
            return IronErrorResponse(404, ERR_404_WORKSPACE_NOT_FOUND)
        ws = services.ensure_personal_workspace(user)
        return IronResponse({"workspace_id": str(ws.id)}, status=status.HTTP_200_OK)
