"""Owner, seat and invitation invariants under concurrent writes (WORK-02).

The audit's finding, in one sentence: every one of these rules was decided
on a snapshot and enforced afterwards, so two requests could each read a
world in which their write was legal and both commit it.

* the last-owner rule was an ``.exists()`` in the view, before the write
  transaction — two demotions each saw the other owner and the workspace
  ended with none;
* the seat ceiling was counted in the view and the invitation rows were
  written after it — two invite batches each saw the last free seat;
* provisioning did not count seats at all;
* nothing stopped two live invitations for one address, and each of them
  reserved a paid seat.

What the fix is: one lock (``services.lock_workspace``, the workspace row),
taken before any of those counts, held until the rows are written, plus a
database constraint for the one invariant a database can state on its own
(one live invitation per address).

**What these tests can and cannot prove here.** The suite runs on SQLite,
where ``SELECT ... FOR UPDATE`` is silently ignored — real parallel
execution needs PostgreSQL (verification playbook §3, "use real PostgreSQL
transactions rather than SQLite for races"). So the tests below pin the two
halves that are provable in-process and that the old code failed:

1. **the decision is re-made under the lock**, driven by committing the
   competing transition inside the window the old code decided from — the
   same "window, reproduced exactly" shape ``TestRevokeIsCompareAndSet``
   already uses for invitations;
2. **the count and the write are one transaction**, so a failed batch
   leaves no half-taken seats;

plus the lock-ordering spy (every mutating path takes the workspace lock,
and takes it before it counts), which is what makes those two hold on a
database that honours the lock.
"""

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from stapel_core.comm.registry import function_registry

from stapel_workspaces import entitlements, services
from stapel_workspaces.errors import (
    ERR_402_MEMBER_LIMIT_REACHED,
    ERR_403_LAST_OWNER,
)
from stapel_workspaces.models import Role, WorkspaceInvitation, WorkspaceMember

BASE = "/workspaces/api/workspaces/v1"


@pytest.fixture
def fake_billing():
    """Scripted ``billing.check_entitlement`` provider (see test_entitlements)."""
    state = {"response": {"allowed": True}, "calls": []}

    def provider(payload):
        state["calls"].append(payload)
        response = state["response"]
        return response(payload) if callable(response) else response

    function_registry.register(entitlements.CHECK_ENTITLEMENT, provider)
    yield state
    function_registry._providers.pop(entitlements.CHECK_ENTITLEMENT, None)
    function_registry._schemas.pop(entitlements.CHECK_ENTITLEMENT, None)


@pytest.fixture
def two_owners(db, user, other_user):
    """A workspace with two owners — the state the last-owner rule guards."""

    class Org:
        pass

    o = Org()
    o.owner = user
    o.second = other_user
    o.ws = services.create_workspace(user=user, name="Acme")
    o.first_member = WorkspaceMember.objects.get(workspace=o.ws, user=user)
    o.second_member = WorkspaceMember.objects.create(
        workspace=o.ws,
        user=other_user,
        role=Role.OWNER,
        accepted_at=timezone.now(),
    )
    return o


@pytest.mark.django_db
class TestLastOwnerIsDecidedUnderTheLock:
    """Two demotions, one owner left: the loser must lose.

    The window is not hypothetical — it is the interval between the view's
    ``.exists()`` ("is there another owner?") and the write, and the whole
    finding is that the answer expires inside it.
    """

    def test_service_refuses_a_demotion_whose_premise_expired(self, two_owners):
        """The caller's snapshot says two owners; by the write there is one."""
        stale = WorkspaceMember.objects.get(pk=two_owners.first_member.pk)
        # The competing transition commits in the window.
        services.change_member_role(
            member=two_owners.second_member,
            new_role=Role.ADMIN,
            actor=two_owners.owner,
        )
        with pytest.raises(services.LastOwnerError):
            services.change_member_role(
                member=stale, new_role=Role.ADMIN, actor=two_owners.owner
            )
        assert (
            WorkspaceMember.objects.filter(
                workspace=two_owners.ws, role=Role.OWNER
            ).count()
            == 1
        )

    def test_service_refuses_a_removal_whose_premise_expired(self, two_owners):
        stale = WorkspaceMember.objects.get(pk=two_owners.first_member.pk)
        services.remove_member(
            member=two_owners.second_member, actor=two_owners.owner
        )
        with pytest.raises(services.LastOwnerError):
            services.remove_member(member=stale, actor=two_owners.owner)
        assert WorkspaceMember.objects.filter(
            workspace=two_owners.ws, role=Role.OWNER
        ).exists()

    def test_endpoint_reports_the_loser_403_not_a_lost_owner(
        self, api_client, two_owners, monkeypatch
    ):
        """The window driven through the HTTP path, with the competitor
        committing after the request has begun.

        Before the fix the view had already answered "another owner exists"
        and wrote regardless: the response was 200 and the workspace was
        left with no owner at all — unadministrable, and repairable only in
        the database.
        """
        real = services.change_member_role

        def demote_the_other_first(*, member, new_role, actor):
            services.change_member_role = real  # once, not on the retry
            real(
                member=two_owners.second_member,
                new_role=Role.ADMIN,
                actor=two_owners.owner,
            )
            return real(member=member, new_role=new_role, actor=actor)

        monkeypatch.setattr(
            services, "change_member_role", demote_the_other_first, raising=False
        )
        api_client.force_authenticate(user=two_owners.owner)
        resp = api_client.patch(
            f"{BASE}/{two_owners.ws.id}/members/{two_owners.owner.id}",
            {"role": "admin"},
            format="json",
        )
        assert resp.status_code == 403
        assert resp.json()["localizable_error"] == ERR_403_LAST_OWNER
        assert WorkspaceMember.objects.filter(
            workspace=two_owners.ws, role=Role.OWNER
        ).exists()

    def test_endpoint_removal_reports_the_loser_403(
        self, api_client, two_owners, monkeypatch
    ):
        real = services.remove_member

        def remove_the_other_first(*, member, actor):
            services.remove_member = real
            real(member=two_owners.second_member, actor=two_owners.owner)
            return real(member=member, actor=actor)

        monkeypatch.setattr(
            services, "remove_member", remove_the_other_first, raising=False
        )
        api_client.force_authenticate(user=two_owners.owner)
        resp = api_client.delete(
            f"{BASE}/{two_owners.ws.id}/members/{two_owners.owner.id}"
        )
        assert resp.status_code == 403
        assert resp.json()["localizable_error"] == ERR_403_LAST_OWNER
        assert WorkspaceMember.objects.filter(
            workspace=two_owners.ws, role=Role.OWNER
        ).exists()


@pytest.mark.django_db
class TestSeatsAreReservedNotJustChecked:
    def test_the_batch_is_one_transaction(self, db, user, monkeypatch):
        """A batch that fails half way leaves no seats taken.

        The old path created invitations one at a time in the view, outside
        any transaction: an error on the third email left two live
        invitations — two reserved seats — behind a 500 the admin reads as
        "nothing happened".
        """
        ws = services.create_workspace(user=user, name="Acme")
        real = services.create_invitation
        seen = []

        def fail_on_the_second(**kwargs):
            seen.append(kwargs["email"])
            if len(seen) == 2:
                raise RuntimeError("mailer exploded")
            return real(**kwargs)

        monkeypatch.setattr(services, "create_invitation", fail_on_the_second)
        with pytest.raises(RuntimeError):
            services.invite_members(
                workspace=ws,
                emails=["a@example.com", "b@example.com"],
                role=Role.MEMBER,
                invited_by=user,
            )
        assert not WorkspaceInvitation.objects.filter(workspace=ws).exists()

    def test_seats_are_counted_after_the_lock_is_taken(
        self, db, user, monkeypatch
    ):
        """Ordering, on every path that spends a seat or an owner.

        A count taken before the lock is a count another transaction is
        free to invalidate; this is the assertion that keeps the two in the
        right order as the code moves.
        """
        order = []
        real_lock = services.lock_workspace
        real_count = entitlements.member_seats_quantity

        def spy_lock(workspace):
            order.append("lock")
            return real_lock(workspace)

        def spy_count(workspace, *, additional=0):
            order.append("count")
            return real_count(workspace, additional=additional)

        monkeypatch.setattr(services, "lock_workspace", spy_lock)
        monkeypatch.setattr(services, "member_seats_quantity", spy_count)

        ws = services.create_workspace(user=user, name="Acme")
        services.invite_members(
            workspace=ws,
            emails=["a@example.com"],
            role=Role.MEMBER,
            invited_by=user,
        )
        assert order and order[0] == "lock"
        assert order.index("lock") < order.index("count")

    def test_two_live_invitations_for_one_address_are_impossible(
        self, db, user
    ):
        """The database says it, not only the service.

        Each live invitation reserves a paid seat, so a duplicate is not a
        cosmetic problem: it is a second working token and a second line on
        the bill for one person.
        """
        ws = services.create_workspace(user=user, name="Acme")
        first = services.create_invitation(
            workspace=ws, email="dup@example.com", role=Role.MEMBER, invited_by=user
        )
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                WorkspaceInvitation.objects.create(
                    workspace=ws,
                    email="dup@example.com",
                    role=Role.MEMBER,
                    invited_by=user,
                    token="a-second-token-for-one-address",
                    expires_at=first.expires_at,
                )

    def test_re_inviting_an_address_refreshes_its_invitation(self, db, user):
        """Invite, invite again: one row, one seat, the newer terms."""
        ws = services.create_workspace(user=user, name="Acme")
        first = services.create_invitation(
            workspace=ws, email="dup@example.com", role=Role.VIEWER, invited_by=user
        )
        again = services.create_invitation(
            workspace=ws,
            email="DUP@example.com",
            role=Role.ADMIN,
            invited_by=user,
            display_name="Ada",
        )
        assert again.pk == first.pk
        assert again.role == Role.ADMIN
        assert again.display_name_hint == "Ada"
        assert WorkspaceInvitation.objects.filter(workspace=ws).count() == 1
        # One row, therefore one seat: the owner plus this invitee.
        assert entitlements.member_seats_quantity(ws) == 2

    def test_a_terminal_invitation_does_not_block_a_new_one(self, db, user):
        """The constraint covers LIVE rows only — history stays whole."""
        ws = services.create_workspace(user=user, name="Acme")
        first = services.create_invitation(
            workspace=ws, email="again@example.com", role=Role.MEMBER, invited_by=user
        )
        services.revoke_invitation(invitation=first, revoked_by=user)
        second = services.create_invitation(
            workspace=ws, email="again@example.com", role=Role.MEMBER, invited_by=user
        )
        assert second.pk != first.pk
        assert WorkspaceInvitation.objects.filter(workspace=ws).count() == 2

    def test_invite_over_the_ceiling_is_refused_by_the_reservation(
        self, authed_client, user, fake_billing
    ):
        """The 402 now comes from the locked reservation, not a pre-check."""
        ws = services.create_workspace(user=user, name="Acme")
        fake_billing["response"] = {"allowed": False, "limit": 1}
        resp = authed_client.post(
            f"{BASE}/{ws.id}/members/invite",
            {"emails": ["a@example.com"], "role": "member"},
            format="json",
        )
        assert resp.status_code == 402
        assert resp.json()["localizable_error"] == ERR_402_MEMBER_LIMIT_REACHED
        assert resp.json()["params"]["limit"] == 1
        assert not WorkspaceInvitation.objects.filter(workspace=ws).exists()


@pytest.mark.django_db
class TestProvisioningSpendsASeat:
    """An org-provisioned member costs a seat like any other member.

    Provisioning checked only the boolean ``workspaces.provision_user``
    entitlement, so an org on a three-seat plan could provision its way to
    any size it liked — the cheapest way past the ceiling was the endpoint
    built for administrators.
    """

    def test_provision_over_the_ceiling_is_refused(
        self, db, user, other_user, fake_billing, monkeypatch
    ):
        ws = services.create_workspace(user=user, name="Acme")

        def verdict(payload):
            if payload["key"] == entitlements.ENT_MEMBERS_MAX:
                return {"allowed": False, "limit": 1}
            return {"allowed": True}

        fake_billing["response"] = verdict
        # A scripted auth that always hands back an account: the point of
        # the test is that the seat, not auth, is what refuses.
        monkeypatch.setattr(
            services,
            "call",
            lambda name, payload=None: {
                "user_id": str(other_user.pk),
                "generated_password": "irrelevant-to-this-test",
            },
        )
        with pytest.raises(entitlements.EntitlementDenied):
            services.provision_member(
                workspace=ws,
                username_local="jdoe",
                role=Role.MEMBER,
                provisioned_by=user,
            )
        assert not WorkspaceMember.objects.filter(
            workspace=ws, provisioned=True
        ).exists()
