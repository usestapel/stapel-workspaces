"""Serializers for workspaces API."""

import re

from rest_framework import serializers
from stapel_core.django.api.errors import StapelValidationError
from stapel_core.django.api.serializers import StapelDataclassSerializer

from .capabilities import effective_roles
from .dto import (
    PROVISIONED_USER_POLICIES,
    InvitationAcceptRequest,
    InvitationClaimResponse,
    InvitationPreviewResponse,
    InvitationResponse,
    MemberInviteRequest,
    MemberInviteResponse,
    MemberPasswordResetRequest,
    MemberPasswordResetResponse,
    MemberResponse,
    MemberUpdateRequest,
    ProvisionMemberRequest,
    ProvisionMemberResponse,
    RoleListResponse,
    RoleResponse,
    WorkspaceCreateRequest,
    WorkspaceListResponse,
    WorkspaceResponse,
    WorkspaceUpdateRequest,
)
from .errors import ERR_400_INVALID_PROVISION_USERNAME, ERR_400_INVALID_ROLE
from .models import Role, WorkspaceType

#: Local (slash-free) username canon — mirrors stapel-auth's
#: ``_LOCAL_USERNAME_RE`` (utils.py): the stock Django username alphabet.
#: Auth re-validates authoritatively in ``auth.provision_user``; this local
#: check exists to fail fast (before billing debits) with a workspaces-keyed
#: 400 instead of a cross-service roundtrip.
_LOCAL_USERNAME_RE = re.compile(r"^[\w.@+-]+\Z")


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

    def validate_settings(self, value):
        # The `security` block is typed (dto.WorkspaceSecuritySettings,
        # spec §C3): the two known keys are validated here, extra keys pass
        # through verbatim (client extension via the serializer seam).
        if value is None:
            return value
        security = value.get("security")
        if security is None:
            return value
        if not isinstance(security, dict):
            raise StapelValidationError(
                "error.400.field.invalid", params={"field": "settings.security"}
            )
        if "require_mfa" in security and not isinstance(
            security["require_mfa"], bool
        ):
            raise StapelValidationError(
                "error.400.field.invalid",
                params={"field": "settings.security.require_mfa"},
            )
        # #90: the policies are an independent SET. The pre-0.13 singular
        # string is still accepted so a client that has not been updated
        # can keep PATCHing; `from_settings` reads either spelling.
        policies = security.get("provisioned_user_policies")
        if policies is not None:
            if not isinstance(policies, list) or any(
                p not in PROVISIONED_USER_POLICIES for p in policies
            ):
                raise StapelValidationError(
                    "error.400.field.invalid_choice",
                    params={"field": "settings.security.provisioned_user_policies"},
                )
        policy = security.get("provisioned_user_policy")
        if policy is not None and policy not in PROVISIONED_USER_POLICIES:
            raise StapelValidationError(
                "error.400.field.invalid_choice",
                params={"field": "settings.security.provisioned_user_policy"},
            )
        return value


class MemberResponseSerializer(StapelDataclassSerializer):
    class Meta:
        dataclass = MemberResponse


class MemberInviteRequestSerializer(StapelDataclassSerializer):
    # Explicit override: the auto-generated field for an `Optional[str] =
    # None` dataclass attribute allows null but not BLANK — a "   " (or ""
    # after normalization) display_name must be accepted and treated as
    # "no hint given" (validate_display_name below), not rejected outright.
    display_name = serializers.CharField(
        required=False, allow_null=True, allow_blank=True, max_length=35
    )

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

    def validate_display_name(self, value):
        # A hint, not the canonical name (that canon — including its own
        # character/emoji rules — lives in stapel-profiles; this module does
        # not import it, per this module's own "never import a sibling
        # module" convention (MODULE.md), so it does not re-derive that
        # validator here). The length ceiling (35, matching stapel-profiles'
        # Profile.display_name) is enforced by the field declaration above;
        # this just normalizes "given but blank" to "no hint" — a stray
        # space in the modal must not read as a distinct choice from
        # leaving the field empty.
        if value is None:
            return value
        value = value.strip()
        return value or None


class MemberInviteResponseSerializer(StapelDataclassSerializer):
    class Meta:
        dataclass = MemberInviteResponse


class InvitationResponseSerializer(StapelDataclassSerializer):
    class Meta:
        dataclass = InvitationResponse


class InvitationPreviewResponseSerializer(StapelDataclassSerializer):
    class Meta:
        dataclass = InvitationPreviewResponse


class InvitationClaimResponseSerializer(StapelDataclassSerializer):
    class Meta:
        dataclass = InvitationClaimResponse


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


class ProvisionMemberRequestSerializer(StapelDataclassSerializer):
    class Meta:
        dataclass = ProvisionMemberRequest

    def validate_username_local(self, value):
        # Fail fast on a malformed local part (workspaces-keyed 400) before
        # any billing debit or auth roundtrip. In particular '/' must not
        # smuggle extra namespace levels into the full
        # "{workspace_slug}/{local}" login. Auth re-validates
        # authoritatively (error.400.username_namespace_invalid).
        value = (value or "").strip()
        if not _LOCAL_USERNAME_RE.match(value) or "/" in value:
            raise StapelValidationError(ERR_400_INVALID_PROVISION_USERNAME)
        return value

    def validate_role(self, value):
        # Effective registry (builtin + settings overlay); provisioning an
        # owner is forbidden, same hardcoded owner protection as invites.
        if value == Role.OWNER or value not in effective_roles():
            raise StapelValidationError(ERR_400_INVALID_ROLE)
        return value


class ProvisionMemberResponseSerializer(StapelDataclassSerializer):
    class Meta:
        dataclass = ProvisionMemberResponse


class MemberPasswordResetRequestSerializer(StapelDataclassSerializer):
    class Meta:
        dataclass = MemberPasswordResetRequest

    def validate_first_login_policies(self, value):
        # #90 vocabulary, same as the workspace security block. Omitted
        # (None) means "the workspace's own policies"; an explicit EMPTY
        # list means "demand nothing", which is a deliberate, auditable
        # choice and not the same thing.
        if value is None:
            return value
        if not isinstance(value, list) or any(
            p not in PROVISIONED_USER_POLICIES for p in value
        ):
            raise StapelValidationError(
                "error.400.field.invalid_choice",
                params={"field": "first_login_policies"},
            )
        return value


class MemberPasswordResetResponseSerializer(StapelDataclassSerializer):
    class Meta:
        dataclass = MemberPasswordResetResponse


class RoleResponseSerializer(StapelDataclassSerializer):
    class Meta:
        dataclass = RoleResponse


class RoleListResponseSerializer(StapelDataclassSerializer):
    class Meta:
        dataclass = RoleListResponse


class InternalPersonalWorkspaceResponseSerializer(serializers.Serializer):
    """Get-or-create result for a user's personal workspace (service-to-service)."""

    workspace_id = serializers.UUIDField(help_text="Personal workspace UUID.")
