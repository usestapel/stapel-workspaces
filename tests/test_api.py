"""API tests for iron-workspaces."""

import pytest

from stapel_workspaces.models import Role, Workspace, WorkspaceMember


@pytest.mark.django_db
class TestWorkspaceCreate:
    def test_create_workspace_seeds_owner_member(self, authed_client, user):
        resp = authed_client.post(
            "/workspaces/api/workspaces",
            {"name": "Acme Eng", "type": "work"},
            format="json",
        )
        assert resp.status_code == 201, resp.content
        data = resp.json()
        assert data["name"] == "Acme Eng"
        assert data["type"] == "work"
        assert data["my_role"] == Role.OWNER
        assert data["member_count"] == 1
        ws = Workspace.objects.get(id=data["id"])
        assert ws.members.filter(user=user, role=Role.OWNER).exists()

    def test_create_slug_taken(self, authed_client, user):
        resp1 = authed_client.post(
            "/workspaces/api/workspaces",
            {"name": "Acme", "slug": "acme"},
            format="json",
        )
        assert resp1.status_code == 201
        resp2 = authed_client.post(
            "/workspaces/api/workspaces",
            {"name": "Acme 2", "slug": "acme"},
            format="json",
        )
        assert resp2.status_code == 400


@pytest.mark.django_db
class TestWorkspaceList:
    def test_list_returns_only_user_workspaces(
        self, api_client, authed_client, user, other_user
    ):
        from stapel_workspaces.services import create_workspace

        mine = create_workspace(user=user, name="Mine")
        create_workspace(user=other_user, name="Theirs")
        resp = authed_client.get("/workspaces/api/workspaces")
        assert resp.status_code == 200
        ids = [w["id"] for w in resp.json()["workspaces"]]
        assert str(mine.id) in ids
        assert len(ids) == 1


@pytest.mark.django_db
class TestWorkspaceDetail:
    def test_non_member_gets_403(self, api_client, user, other_user):
        from stapel_workspaces.services import create_workspace

        ws = create_workspace(user=other_user, name="Theirs")
        api_client.force_authenticate(user=user)
        resp = api_client.get(f"/workspaces/api/workspaces/{ws.id}")
        assert resp.status_code == 403

    def test_owner_can_delete(self, authed_client, user):
        from stapel_workspaces.services import create_workspace

        ws = create_workspace(user=user, name="Doomed")
        resp = authed_client.delete(f"/workspaces/api/workspaces/{ws.id}")
        assert resp.status_code == 204
        ws.refresh_from_db()
        assert ws.deleted_at is not None


@pytest.mark.django_db
class TestMembershipInvite:
    def test_admin_can_invite(self, authed_client, user):
        from stapel_workspaces.services import create_workspace

        ws = create_workspace(user=user, name="A")
        resp = authed_client.post(
            f"/workspaces/api/workspaces/{ws.id}/members/invite",
            {"emails": ["new@example.com"], "role": "member"},
            format="json",
        )
        assert resp.status_code == 201
        assert resp.json()["invitations"][0]["email"] == "new@example.com"
        assert ws.invitations.count() == 1

    def test_non_admin_cannot_invite(
        self, api_client, authed_client, user, other_user
    ):
        from stapel_workspaces.services import create_workspace

        ws = create_workspace(user=other_user, name="Theirs")
        WorkspaceMember.objects.create(
            workspace=ws, user=user, role=Role.MEMBER, accepted_at=ws.created_at
        )
        resp = authed_client.post(
            f"/workspaces/api/workspaces/{ws.id}/members/invite",
            {"emails": ["new@example.com"], "role": "member"},
            format="json",
        )
        assert resp.status_code == 403

    def test_last_owner_cannot_be_removed(self, authed_client, user, other_user):
        from stapel_workspaces.services import create_workspace

        ws = create_workspace(user=user, name="A")
        resp = authed_client.delete(
            f"/workspaces/api/workspaces/{ws.id}/members/{user.id}"
        )
        assert resp.status_code == 403
