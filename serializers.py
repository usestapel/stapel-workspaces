"""Serializers for workspaces API."""

from rest_framework import serializers
from stapel_core.django.api.errors import StapelValidationError
from stapel_core.django.api.serializers import StapelDataclassSerializer

from .dto import (
    InvitationAcceptRequest,
    InvitationResponse,
    MemberInviteRequest,
    MemberInviteResponse,
    MemberListResponse,
    MemberResponse,
    MemberUpdateRequest,
    WorkspaceCreateRequest,
    WorkspaceListResponse,
    WorkspaceResponse,
    WorkspaceUpdateRequest,
)
from .errors import ERR_400_INVALID_ROLE
from .models import Role, WorkspaceType


class WorkspaceResponseSerializer(StapelDataclassSerializer):
    class Meta:
        dataclass = WorkspaceResponse


class WorkspaceListResponseSerializer(StapelDataclassSerializer):
    class Meta:
        dataclass = WorkspaceListResponse


class WorkspaceCreateRequestSerializer(StapelDataclassSerializer):
    class Meta:
        dataclass = WorkspaceCreateRequest

    def validate_type(self, value):
        if value not in WorkspaceType.values:
            raise StapelValidationError(ERR_400_INVALID_ROLE)
        return value


class WorkspaceUpdateRequestSerializer(StapelDataclassSerializer):
    class Meta:
        dataclass = WorkspaceUpdateRequest


class MemberResponseSerializer(StapelDataclassSerializer):
    class Meta:
        dataclass = MemberResponse


class MemberListResponseSerializer(StapelDataclassSerializer):
    class Meta:
        dataclass = MemberListResponse


class MemberInviteRequestSerializer(StapelDataclassSerializer):
    class Meta:
        dataclass = MemberInviteRequest

    def validate_role(self, value):
        if value not in {Role.ADMIN, Role.MEMBER, Role.VIEWER}:
            raise StapelValidationError(ERR_400_INVALID_ROLE)
        return value

    def validate_emails(self, value):
        if not value:
            raise serializers.ValidationError("At least one email is required")
        return [e.lower().strip() for e in value]


class MemberInviteResponseSerializer(StapelDataclassSerializer):
    class Meta:
        dataclass = MemberInviteResponse


class InvitationResponseSerializer(StapelDataclassSerializer):
    class Meta:
        dataclass = InvitationResponse


class InvitationAcceptRequestSerializer(StapelDataclassSerializer):
    class Meta:
        dataclass = InvitationAcceptRequest


class MemberUpdateRequestSerializer(StapelDataclassSerializer):
    class Meta:
        dataclass = MemberUpdateRequest

    def validate_role(self, value):
        if value not in Role.values:
            raise StapelValidationError(ERR_400_INVALID_ROLE)
        return value


class InternalPersonalWorkspaceResponseSerializer(serializers.Serializer):
    """Get-or-create result for a user's personal workspace (service-to-service)."""

    workspace_id = serializers.UUIDField(help_text="Personal workspace UUID.")
