"""Tests for the package-level public API (__all__, lazy PEP 562 exports)
and the serializer seams on the API views."""

import pytest
from rest_framework.test import APIRequestFactory, force_authenticate

import stapel_workspaces


class TestPublicAPI:
    def test_all_matches_exports(self):
        assert sorted(stapel_workspaces.__all__) == sorted(
            [
                "create_workspace",
                "ensure_personal_workspace",
                "create_invitation",
                "accept_invitation",
                "CHECK_MEMBERSHIP",
                "check_membership",
                "EVENT_WORKSPACE_PERSONAL_CREATED",
                "WorkspacesGDPRProvider",
            ]
        )

    def test_lazy_exports_resolve(self):
        from stapel_workspaces import functions, gdpr, services

        assert stapel_workspaces.create_workspace is services.create_workspace
        assert (
            stapel_workspaces.ensure_personal_workspace
            is services.ensure_personal_workspace
        )
        assert stapel_workspaces.create_invitation is services.create_invitation
        assert stapel_workspaces.accept_invitation is services.accept_invitation
        assert stapel_workspaces.check_membership is functions.check_membership
        assert (
            stapel_workspaces.CHECK_MEMBERSHIP == "workspaces.check_membership"
        )
        assert (
            stapel_workspaces.WorkspacesGDPRProvider
            is gdpr.WorkspacesGDPRProvider
        )

    def test_dir_lists_exports(self):
        assert set(stapel_workspaces.__all__) <= set(dir(stapel_workspaces))

    def test_unknown_attribute_raises(self):
        with pytest.raises(AttributeError):
            stapel_workspaces.does_not_exist


@pytest.mark.django_db
class TestSerializerSeams:
    def test_subclass_can_swap_response_serializer(self, user):
        """A view subclass overriding response_serializer_class changes the
        payload without touching the method bodies."""
        from stapel_workspaces.serializers import MemberListResponseSerializer
        from stapel_workspaces.views import MemberDetailView, MemberListView

        class StampedSerializer(MemberListResponseSerializer):
            def to_representation(self, instance):
                data = super().to_representation(instance)
                data["swapped"] = True
                return data

        class StampedMemberListView(MemberListView):
            response_serializer_class = StampedSerializer

        ws = stapel_workspaces.create_workspace(user=user, name="Seams")

        factory = APIRequestFactory()
        request = factory.get("/x/members")
        force_authenticate(request, user=user)
        resp = StampedMemberListView.as_view()(request, workspace_id=ws.id)
        assert resp.status_code == 200
        assert resp.data["swapped"] is True
        assert len(resp.data["members"]) == 1

        # Defaults are exposed as class attributes on every view.
        assert (
            MemberDetailView().get_request_serializer_class()
            is MemberDetailView.request_serializer_class
        )
        assert (
            MemberDetailView().get_response_serializer_class()
            is MemberDetailView.response_serializer_class
        )
