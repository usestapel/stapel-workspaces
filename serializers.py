"""Serializers for workspaces API."""

from rest_framework import serializers
from stapel_core.django.api.errors import StapelValidationError
from stapel_core.django.api.serializers import IronDataclassSerializer

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


class WorkspaceResponseSerializer(IronDataclassSerializer):
    class Meta:
        dataclass = WorkspaceResponse


class WorkspaceListResponseSerializer(IronDataclassSerializer):
    class Meta:
        dataclass = WorkspaceListResponse


class WorkspaceCreateRequestSerializer(IronDataclassSerializer):
    class Meta:
        dataclass = WorkspaceCreateRequest

    def validate_type(self, value):
        if value not in WorkspaceType.values:
            raise StapelValidationError(ERR_400_INVALID_ROLE)
        return value


class WorkspaceUpdateRequestSerializer(IronDataclassSerializer):
    class Meta:
        dataclass = WorkspaceUpdateRequest


class MemberResponseSerializer(IronDataclassSerializer):
    class Meta:
        dataclass = MemberResponse


class MemberListResponseSerializer(IronDataclassSerializer):
    class Meta:
        dataclass = MemberListResponse


class MemberInviteRequestSerializer(IronDataclassSerializer):
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


class MemberInviteResponseSerializer(IronDataclassSerializer):
    class Meta:
        dataclass = MemberInviteResponse


class InvitationResponseSerializer(IronDataclassSerializer):
    class Meta:
        dataclass = InvitationResponse


class InvitationAcceptRequestSerializer(IronDataclassSerializer):
    class Meta:
        dataclass = InvitationAcceptRequest


class MemberUpdateRequestSerializer(IronDataclassSerializer):
    class Meta:
        dataclass = MemberUpdateRequest

    def validate_role(self, value):
        if value not in Role.values:
            raise StapelValidationError(ERR_400_INVALID_ROLE)
        return value
