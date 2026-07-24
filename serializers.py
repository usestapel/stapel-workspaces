"""Serializers for workspaces API."""

from rest_framework import serializers
from stapel_core.django.api.errors import StapelValidationError
from stapel_core.django.api.serializers import StapelDataclassSerializer

from .capabilities import effective_roles
from .dto import (
    InvitationAcceptRequest,
    InvitationResponse,
    MemberInviteRequest,
    MemberInviteResponse,
    MemberResponse,
    MemberUpdateRequest,
    RoleListResponse,
    RoleResponse,
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


class MemberInviteRequestSerializer(StapelDataclassSerializer):
    class Meta:
        dataclass = MemberInviteRequest

    def validate_role(self, value):
        # Validated against the EFFECTIVE registry (builtin + settings
        # overlay) — custom product roles are invitable; granting `owner`
        # via invitation stays forbidden (hardcoded owner protection).
        if value == Role.OWNER or value not in effective_roles():
            raise StapelValidationError(ERR_400_INVALID_ROLE)
        return value

    def validate_emails(self, value):
        if not value:
            raise serializers.ValidationError("At least one email is required")  # noqa: R002
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
        # Effective registry, not the hardcoded four: custom product roles
        # are assignable. Granting `owner` remains possible here — the view
        # gates it on the requester being an owner.
        if value not in effective_roles():
            raise StapelValidationError(ERR_400_INVALID_ROLE)
        return value


class RoleResponseSerializer(StapelDataclassSerializer):
    class Meta:
        dataclass = RoleResponse


class RoleListResponseSerializer(StapelDataclassSerializer):
    class Meta:
        dataclass = RoleListResponse


class InternalPersonalWorkspaceResponseSerializer(serializers.Serializer):
    """Get-or-create result for a user's personal workspace (service-to-service)."""

    workspace_id = serializers.UUIDField(help_text="Personal workspace UUID.")
