"""API tests for member list / update / removal, incl. owner-escalation guards."""

import uuid

import pytest
from django.utils import timezone

from stapel_workspaces.errors import (
    ERR_400_INVALID_ROLE,
    ERR_403_FORBIDDEN_WORKSPACE,
    ERR_403_LAST_OWNER,
    ERR_404_MEMBER_NOT_FOUND,
)
from stapel_workspaces.models import Role, WorkspaceMember

BASE = "/workspaces/api/workspaces"


@pytest.fixture
def third_user(db):
    from stapel_core.django.users.models import User

    return User.objects.create_user(
        username=f"u-{uuid.uuid4().hex[:8]}",
        email=f"{uuid.uuid4().hex[:8]}@example.com",
        password="testpass-1234",
    )


def _create_ws(user, name="Acme"):
    from stapel_workspaces.services import create_workspace

    return create_workspace(user=user, name=name)


def _add_member(ws, user, role):
    return WorkspaceMember.objects.create(
        workspace=ws, user=user, role=role, accepted_at=timezone.now()
    )


def _named_user(first, last, email):
    from stapel_core.django.users.models import User

    return User.objects.create_user(
        username=f"u-{uuid.uuid4().hex[:8]}",
        email=email,
        first_name=first,
        last_name=last,
        password="testpass-1234",
    )


@pytest.mark.django_db
class TestMemberList:
    def test_requires_auth(self, api_client, user):
        ws = _create_ws(user)
        assert api_client.get(f"{BASE}/{ws.id}/members").status_code in (401, 403)

    def test_member_can_list(self, api_client, user, other_user):
        ws = _create_ws(other_user)
        _add_member(ws, user, Role.VIEWER)
        api_client.force_authenticate(user=user)
        resp = api_client.get(f"{BASE}/{ws.id}/members")
        assert resp.status_code == 200, resp.content
        members = resp.json()["items"]
        assert len(members) == 2
        by_user = {m["user_id"]: m for m in members}
        assert by_user[str(other_user.id)]["role"] == Role.OWNER
        assert by_user[str(user.id)]["role"] == Role.VIEWER
        assert by_user[str(user.id)]["email"] == user.email

    def test_non_member_403(self, authed_client, other_user):
        ws = _create_ws(other_user)
        resp = authed_client.get(f"{BASE}/{ws.id}/members")
        assert resp.status_code == 403


def _stamp_invited_at(member, when):
    """Force a member's ``invited_at`` (``auto_now_add`` normally sets it).

    The anchor is ``-invited_at``; to make ordering / window seams
    deterministic the tests assign explicit, distinct creation timestamps.
    """
    WorkspaceMember.objects.filter(pk=member.pk).update(invited_at=when)
    member.invited_at = when
    return member


@pytest.mark.django_db
class TestMemberListSearchPagination:
    """?search= + anchor pagination (stapel-core mandate; BACKLOG G12).

    The former display-name sort is gone: ``AnchorPagination`` supports only a
    single monotonic anchor (no composite ``name,id``), so the list is ordered
    by the ``-invited_at`` cursor — consistency with the whole-codebase
    limit/offset ban wins over name ordering (CHANGELOG 0.4.0).
    """

    def test_search_by_email(self, authed_client, user):
        ws = _create_ws(user)
        alice = _named_user("Alice", "Anderson", "alice@picker.test")
        _add_member(ws, alice, Role.MEMBER)
        _add_member(ws, _named_user("Bob", "Brown", "bob@picker.test"), Role.MEMBER)
        resp = authed_client.get(f"{BASE}/{ws.id}/members?search=ALICE@pick")
        assert resp.status_code == 200, resp.content
        ids = [m["user_id"] for m in resp.json()["items"]]
        assert ids == [str(alice.id)]

    def test_search_by_display_name_case_insensitive(self, authed_client, user):
        ws = _create_ws(user)
        alice = _named_user("Alice", "Anderson", "alice@picker.test")
        _add_member(ws, alice, Role.MEMBER)
        _add_member(ws, _named_user("Bob", "Brown", "bob@picker.test"), Role.MEMBER)
        # last name
        resp = authed_client.get(f"{BASE}/{ws.id}/members?search=anderson")
        assert [m["user_id"] for m in resp.json()["items"]] == [str(alice.id)]
        # full display name ("Alice Anderson") also matches
        resp2 = authed_client.get(f"{BASE}/{ws.id}/members?search=alice ander")
        assert [m["user_id"] for m in resp2.json()["items"]] == [str(alice.id)]

    def test_search_empty_result(self, authed_client, user):
        ws = _create_ws(user)
        _add_member(
            ws, _named_user("Alice", "Anderson", "alice@picker.test"), Role.MEMBER
        )
        resp = authed_client.get(f"{BASE}/{ws.id}/members?search=nobody-zzz")
        assert resp.status_code == 200
        body = resp.json()
        assert body["items"] == []
        assert body["has_next"] is False

    def test_order_is_by_invited_at_anchor_newest_first(self, authed_client, user):
        """Output order is the module anchor (-invited_at), not display name."""
        ws = _create_ws(user)
        cara = _add_member(ws, _named_user("Cara", "C", "cara@picker.test"), Role.MEMBER)
        anna = _add_member(ws, _named_user("Anna", "A", "anna@picker.test"), Role.MEMBER)
        bella = _add_member(ws, _named_user("Bella", "B", "bella@picker.test"), Role.MEMBER)
        t0 = timezone.now()
        # Insertion order cara, anna, bella — stamp so newest-first is bella,
        # anna, cara (deliberately NOT alphabetical, to prove name sort is gone).
        _stamp_invited_at(cara, t0)
        _stamp_invited_at(anna, t0 + timezone.timedelta(minutes=1))
        _stamp_invited_at(bella, t0 + timezone.timedelta(minutes=2))
        resp = authed_client.get(f"{BASE}/{ws.id}/members?search=picker.test")
        ids = [m["user_id"] for m in resp.json()["items"]]
        assert ids == [str(bella.user_id), str(anna.user_id), str(cara.user_id)]

    def test_first_page_and_next_by_anchor(self, authed_client, user):
        ws = _create_ws(user)
        base = f"{BASE}/{ws.id}/members"
        # 4 members + owner; stamp distinct, ascending invited_at.
        t0 = timezone.now()
        members = []
        for i in range(4):
            m = _add_member(
                ws, _named_user(f"M{i}", "X", f"m{i}@picker.test"), Role.MEMBER
            )
            _stamp_invited_at(m, t0 + timezone.timedelta(minutes=i + 1))
            members.append(m)
        # newest-first traversal: m3, m2, m1, m0, owner
        full = [m["user_id"] for m in authed_client.get(base).json()["items"]]
        assert len(full) == 5

        p1 = authed_client.get(base, {"limit": 2}).json()
        assert [m["user_id"] for m in p1["items"]] == full[:2]
        assert p1["has_next"] is True
        assert p1["next_anchor"]

        # anchor is passed via the params dict so its "+00:00" tz offset is
        # URL-encoded (a bare "+" in a query string would decode to a space).
        p2 = authed_client.get(base, {"limit": 2, "anchor": p1["next_anchor"]}).json()
        assert [m["user_id"] for m in p2["items"]] == full[2:4]

        p3 = authed_client.get(base, {"limit": 2, "anchor": p2["next_anchor"]}).json()
        assert [m["user_id"] for m in p3["items"]] == full[4:]
        assert p3["has_next"] is False

    def test_window_seam_holds_under_insert_no_skip_no_dupe(self, authed_client, user):
        """THE rule: a row inserted into the already-served range must not shift
        the next window. Offset pagination would dupe/skip here; the anchor
        resumes from a value, so page 2 is exactly m2's successors.
        """
        ws = _create_ws(user)
        base = f"{BASE}/{ws.id}/members"
        t0 = timezone.now()
        # invited_at: owner oldest (created with ws), then m0..m3 ascending.
        m = {}
        for i in range(4):
            mm = _add_member(
                ws, _named_user(f"M{i}", "X", f"m{i}@picker.test"), Role.MEMBER
            )
            # spacing of 10 min leaves room to insert a row *between* two of them
            _stamp_invited_at(mm, t0 + timezone.timedelta(minutes=10 * (i + 1)))
            m[i] = mm
        # newest-first: m3, m2, m1, m0, owner
        p1 = authed_client.get(base, {"limit": 2}).json()
        page1_ids = [x["user_id"] for x in p1["items"]]
        assert page1_ids == [str(m[3].user_id), str(m[2].user_id)]

        # Insert a NEW member INTO the already-served range: invited_at between
        # m2 and m3 (i.e. it logically belongs on page 1). Under limit/offset,
        # page-2-at-offset-2 would now re-serve m2 (a duplicate); the anchor
        # must not.
        intruder = _add_member(
            ws, _named_user("Zed", "Z", "zed@picker.test"), Role.MEMBER
        )
        _stamp_invited_at(
            intruder, t0 + timezone.timedelta(minutes=35)
        )  # between m2 (30) and m3 (40) — inside the range page 1 already served

        p2 = authed_client.get(base, {"limit": 2, "anchor": p1["next_anchor"]}).json()
        page2_ids = [x["user_id"] for x in p2["items"]]
        # Exactly m2's true successors — no dupe of m2, no skip of m1.
        assert page2_ids == [str(m[1].user_id), str(m[0].user_id)]
        assert set(page1_ids).isdisjoint(page2_ids)
        assert str(intruder.user_id) not in page2_ids  # it belonged on page 1

    def test_junk_limit_falls_back_to_page_size(self, authed_client, user):
        ws = _create_ws(user)
        _add_member(
            ws, _named_user("Alice", "Anderson", "alice@picker.test"), Role.MEMBER
        )
        resp = authed_client.get(f"{BASE}/{ws.id}/members?limit=abc")
        assert resp.status_code == 200
        assert len(resp.json()["items"]) == 2  # owner + member, junk limit ignored

    def test_no_params_returns_first_page(self, authed_client, user):
        ws = _create_ws(user)
        _add_member(
            ws, _named_user("Alice", "Anderson", "alice@picker.test"), Role.MEMBER
        )
        resp = authed_client.get(f"{BASE}/{ws.id}/members")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["items"]) == 2  # owner + member (below page_size)
        assert body["has_next"] is False
        assert body["has_prev"] is False

    def test_search_foreign_workspace_403(self, api_client, user, other_user):
        ws = _create_ws(other_user)
        api_client.force_authenticate(user=user)
        resp = api_client.get(f"{BASE}/{ws.id}/members?search=alice")
        assert resp.status_code == 403


@pytest.mark.django_db
class TestMemberUpdate:
    def test_requires_auth(self, api_client, user, other_user):
        ws = _create_ws(user)
        resp = api_client.patch(
            f"{BASE}/{ws.id}/members/{other_user.id}",
            {"role": "admin"},
            format="json",
        )
        assert resp.status_code in (401, 403)

    def test_owner_can_change_role(self, authed_client, user, other_user):
        ws = _create_ws(user)
        member = _add_member(ws, other_user, Role.MEMBER)
        resp = authed_client.patch(
            f"{BASE}/{ws.id}/members/{other_user.id}",
            {"role": "admin"},
            format="json",
        )
        assert resp.status_code == 200, resp.content
        assert resp.json()["role"] == Role.ADMIN
        member.refresh_from_db()
        assert member.role == Role.ADMIN

    def test_admin_cannot_grant_owner(self, api_client, user, other_user):
        """Owner-escalation guard: an admin must not mint owners (incl. self)."""
        ws = _create_ws(other_user)
        me = _add_member(ws, user, Role.ADMIN)
        api_client.force_authenticate(user=user)
        resp = api_client.patch(
            f"{BASE}/{ws.id}/members/{user.id}", {"role": "owner"}, format="json"
        )
        assert resp.status_code == 403
        assert resp.json()["localizable_error"] == ERR_403_FORBIDDEN_WORKSPACE
        me.refresh_from_db()
        assert me.role == Role.ADMIN

    def test_admin_cannot_change_owner_role(self, api_client, user, other_user):
        ws = _create_ws(other_user)
        _add_member(ws, user, Role.ADMIN)
        api_client.force_authenticate(user=user)
        resp = api_client.patch(
            f"{BASE}/{ws.id}/members/{other_user.id}",
            {"role": "member"},
            format="json",
        )
        assert resp.status_code == 403
        assert (
            WorkspaceMember.objects.get(workspace=ws, user=other_user).role
            == Role.OWNER
        )

    def test_sole_owner_cannot_be_demoted(self, authed_client, user):
        ws = _create_ws(user)
        resp = authed_client.patch(
            f"{BASE}/{ws.id}/members/{user.id}", {"role": "admin"}, format="json"
        )
        assert resp.status_code == 403
        assert resp.json()["localizable_error"] == ERR_403_LAST_OWNER

    def test_owner_can_be_demoted_when_another_owner_exists(
        self, authed_client, user, other_user
    ):
        ws = _create_ws(user)
        _add_member(ws, other_user, Role.OWNER)
        resp = authed_client.patch(
            f"{BASE}/{ws.id}/members/{user.id}", {"role": "admin"}, format="json"
        )
        assert resp.status_code == 200
        assert (
            WorkspaceMember.objects.get(workspace=ws, user=user).role == Role.ADMIN
        )

    def test_invalid_role_rejected(self, authed_client, user, other_user):
        ws = _create_ws(user)
        _add_member(ws, other_user, Role.MEMBER)
        resp = authed_client.patch(
            f"{BASE}/{ws.id}/members/{other_user.id}",
            {"role": "supreme-leader"},
            format="json",
        )
        assert resp.status_code == 400
        assert ERR_400_INVALID_ROLE in str(resp.json())

    def test_member_not_found_404(self, authed_client, user, other_user):
        ws = _create_ws(user)
        resp = authed_client.patch(
            f"{BASE}/{ws.id}/members/{other_user.id}",
            {"role": "member"},
            format="json",
        )
        assert resp.status_code == 404
        assert resp.json()["localizable_error"] == ERR_404_MEMBER_NOT_FOUND

    def test_non_admin_cannot_update(self, api_client, user, other_user, third_user):
        ws = _create_ws(other_user)
        _add_member(ws, user, Role.MEMBER)
        _add_member(ws, third_user, Role.MEMBER)
        api_client.force_authenticate(user=user)
        resp = api_client.patch(
            f"{BASE}/{ws.id}/members/{third_user.id}",
            {"role": "viewer"},
            format="json",
        )
        assert resp.status_code == 403


@pytest.mark.django_db
class TestMemberRemove:
    def test_requires_auth(self, api_client, user, other_user):
        ws = _create_ws(user)
        _add_member(ws, other_user, Role.MEMBER)
        resp = api_client.delete(f"{BASE}/{ws.id}/members/{other_user.id}")
        assert resp.status_code in (401, 403)

    def test_admin_can_remove_member(self, api_client, user, other_user, third_user):
        ws = _create_ws(other_user)
        _add_member(ws, user, Role.ADMIN)
        _add_member(ws, third_user, Role.MEMBER)
        api_client.force_authenticate(user=user)
        resp = api_client.delete(f"{BASE}/{ws.id}/members/{third_user.id}")
        assert resp.status_code == 204
        assert not WorkspaceMember.objects.filter(
            workspace=ws, user=third_user
        ).exists()

    def test_admin_cannot_remove_owner(self, api_client, user, other_user):
        ws = _create_ws(other_user)
        _add_member(ws, user, Role.ADMIN)
        api_client.force_authenticate(user=user)
        resp = api_client.delete(f"{BASE}/{ws.id}/members/{other_user.id}")
        assert resp.status_code == 403
        assert WorkspaceMember.objects.filter(
            workspace=ws, user=other_user
        ).exists()

    def test_owner_can_remove_co_owner(self, authed_client, user, other_user):
        ws = _create_ws(user)
        _add_member(ws, other_user, Role.OWNER)
        resp = authed_client.delete(f"{BASE}/{ws.id}/members/{other_user.id}")
        assert resp.status_code == 204
        assert not WorkspaceMember.objects.filter(
            workspace=ws, user=other_user
        ).exists()

    def test_remove_unknown_member_404(self, authed_client, user, other_user):
        ws = _create_ws(user)
        resp = authed_client.delete(f"{BASE}/{ws.id}/members/{other_user.id}")
        assert resp.status_code == 404
