"""The admin side of an invitation: see it, withdraw it, send it again (#109).

The gap this closes. Sending an invitation was a write-only act: acceptance
produced a member row, and every other outcome — the letter that bounced,
the address with a typo in it, the person who declined, the invite that
quietly expired — left nothing an admin could look at. The roster answered
"who is in". Nothing answered "who is still out, and since when", and
nothing let an admin act on the answer.

Three endpoints, one mandate (``members.invite``):

* ``GET  {ws}/invitations`` — pending by default, ``never_accepted`` and
  ``all`` on request. Anchor-paginated on ``created_at``.
* ``POST {ws}/invitations/{id}/revoke`` — the workspace's terminal "no",
  the mirror of the invitee's decline; frees the seat.
* ``POST {ws}/invitations/{id}/resend`` — restarts the TTL and re-mails,
  no more often than the cooldown allows. Deliberately accepts an EXPIRED
  invitation; deliberately keeps the token the invitee already has.

What the tests below are actually defending, beyond "the route answers":

**The filters are the library's predicates, not new ones.** The list
vocabulary maps onto ``InvitationQuerySet.pending()`` /
``.never_accepted()`` — the same formulas the seat count and the GDPR
erasure read. ``tests/test_lifecycle_predicates.py`` bans a hand-written
copy anywhere outside ``models.py``; :class:`TestFiltersAreTheCanonicalPredicates`
pins the other half, that these filters *select what those predicates
select* rather than merely avoiding the banned spelling.

**Revoke is a compare-and-set, not a write.** The view checks the state and
the service locks the row: two reads, and the gap between them is where a
concurrent accept lives. 0.10.0 fixed that race in the other direction (a
revocation committing between accept's check and accept's lock). The
revoke half is pinned here by driving the accept *inside* the window,
:meth:`TestRevokeIsCompareAndSet.test_accept_committed_in_the_window_wins`.

**The seat comes back.** Revocation is worth money: a pending invitation
reserves a paid seat, so an org that invited ten people and had two answer
must not go on paying for eight forever.
"""

import json
from datetime import timedelta
from pathlib import Path

import jsonschema
import pytest
from django.test import override_settings
from django.utils import timezone
from stapel_core.comm import subscribe_action

import stapel_workspaces
from stapel_workspaces import entitlements, services
from stapel_workspaces.errors import (
    ERR_400_INVITATION_ALREADY_USED,
    ERR_400_INVITATION_DECLINED,
    ERR_400_INVITATION_EXPIRED,
    ERR_400_INVITATION_REVOKED,
    ERR_403_MISSING_CAPABILITY,
    ERR_404_INVITATION_NOT_FOUND,
    ERR_404_WORKSPACE_NOT_FOUND,
    ERR_429_INVITATION_RESEND_COOLDOWN,
)
from stapel_workspaces.models import (
    InvitationStatus,
    Role,
    WorkspaceInvitation,
    WorkspaceMember,
)

BASE = "/workspaces/api/workspaces/v1"


def _invitations_url(ws, **params):
    url = f"{BASE}/{ws.id}/invitations"
    if params:
        url += "?" + "&".join(f"{k}={v}" for k, v in params.items())
    return url


def _invite(ws, email, inviter, role=Role.MEMBER, **overrides):
    inv = services.create_invitation(
        workspace=ws, email=email, role=role, invited_by=inviter
    )
    if overrides:
        for key, value in overrides.items():
            setattr(inv, key, value)
        inv.save(update_fields=list(overrides))
    return inv


def _time_passes(inv, seconds=3600):
    """Move this invitation's last-sent clock into the past.

    Creating an invitation mails it, which starts the resend cooldown — so
    "an admin resends" is, in every realistic case, "an admin resends
    LATER". Tests that are about resend rather than about the cooldown say
    so with this instead of quietly running with the limiter disabled.
    """
    inv.last_sent_at = timezone.now() - timedelta(seconds=seconds)
    inv.save(update_fields=["last_sent_at"])
    return inv


@pytest.fixture
def org(db, user, other_user):
    """One workspace whose invitations sit in every lifecycle state."""

    class Org:
        pass

    o = Org()
    o.owner = user
    o.outsider = other_user
    o.ws = services.create_workspace(user=o.owner, name="Acme")
    o.pending = _invite(o.ws, "pending@example.com", o.owner)
    o.declined = _invite(
        o.ws, "declined@example.com", o.owner, declined_at=timezone.now()
    )
    o.revoked = _invite(
        o.ws, "revoked@example.com", o.owner, revoked_at=timezone.now()
    )
    o.expired = _invite(
        o.ws,
        "expired@example.com",
        o.owner,
        expires_at=timezone.now() - timedelta(days=1),
    )
    o.accepted = _invite(
        o.ws, "accepted@example.com", o.owner, accepted_at=timezone.now()
    )
    return o


@pytest.fixture
def admin_client(api_client, org):
    api_client.force_authenticate(user=org.owner)
    return api_client


@pytest.fixture
def revocations():
    """Collect ``workspace.invitation_revoked`` payloads off the comm bus."""
    events = []
    subscribe_action("workspace.invitation_revoked", events.append)
    return events


# ---------------------------------------------------------------------------
# GET {ws}/invitations — the answer to "who has not accepted"
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestInvitationList:
    def test_pending_is_the_default_and_the_point(self, admin_client, org):
        """The whole request: an admin opens the screen and sees who is out.

        No query parameter, because the default has to be the useful one —
        an admin who must know to ask for ``?status=pending`` is an admin
        who sees a list of ghosts on first load.
        """
        body = admin_client.get(_invitations_url(org.ws)).json()
        assert [i["email"] for i in body["items"]] == ["pending@example.com"]
        row = body["items"][0]
        assert row["status"] == InvitationStatus.PENDING
        assert row["role"] == Role.MEMBER
        assert row["invited_by_id"] == str(org.owner.id)
        assert row["accepted_at"] is None

    def test_never_accepted_shows_the_ones_that_died(self, admin_client, org):
        """The audit view: pending + declined + revoked + expired.

        An admin chasing a hire needs to see the decline — "no invitation"
        and "they said no" are different facts and produce different next
        actions.
        """
        body = admin_client.get(
            _invitations_url(org.ws, status="never_accepted")
        ).json()
        assert {i["email"] for i in body["items"]} == {
            "pending@example.com",
            "declined@example.com",
            "revoked@example.com",
            "expired@example.com",
        }
        by_email = {i["email"]: i for i in body["items"]}
        assert by_email["declined@example.com"]["status"] == InvitationStatus.DECLINED
        assert by_email["revoked@example.com"]["status"] == InvitationStatus.REVOKED
        assert by_email["expired@example.com"]["status"] == InvitationStatus.EXPIRED
        assert by_email["declined@example.com"]["declined_at"] is not None

    def test_all_includes_the_accepted_ones(self, admin_client, org):
        body = admin_client.get(_invitations_url(org.ws, status="all")).json()
        assert len(body["items"]) == 5
        accepted = next(
            i for i in body["items"] if i["email"] == "accepted@example.com"
        )
        assert accepted["status"] == InvitationStatus.ACCEPTED

    def test_unknown_status_is_a_keyed_400_not_a_silent_default(
        self, admin_client, org
    ):
        """A typo must not answer with somebody else's list."""
        resp = admin_client.get(_invitations_url(org.ws, status="peding"))
        assert resp.status_code == 400
        assert resp.json()["localizable_error"] == "error.400.field.invalid_choice"

    def test_search_narrows_by_email(self, admin_client, org):
        body = admin_client.get(
            _invitations_url(org.ws, status="all", search="ACCEPT")
        ).json()
        assert [i["email"] for i in body["items"]] == ["accepted@example.com"]

    def test_token_never_appears_in_the_response(self, admin_client, org):
        """The invite token is a bearer credential.

        A list endpoint that carried it would hand every admin a working
        sign-in link for every address they ever invited.
        """
        raw = admin_client.get(_invitations_url(org.ws, status="all")).content
        for inv in WorkspaceInvitation.objects.all():
            assert inv.token.encode() not in raw
        assert b"token" not in raw

    def test_member_without_the_mandate_is_refused(self, api_client, org):
        """`members.invite`, not "is logged in" — the roster is not the list."""
        WorkspaceMember.objects.create(
            workspace=org.ws,
            user=org.outsider,
            role=Role.MEMBER,
            accepted_at=timezone.now(),
        )
        api_client.force_authenticate(user=org.outsider)
        resp = api_client.get(_invitations_url(org.ws))
        assert resp.status_code == 403
        assert resp.json()["localizable_error"] == ERR_403_MISSING_CAPABILITY

    def test_non_member_cannot_read_another_org_invitations(self, api_client, org):
        api_client.force_authenticate(user=org.outsider)
        assert api_client.get(_invitations_url(org.ws)).status_code == 403

    def test_anonymous_is_refused(self, api_client, org):
        assert api_client.get(_invitations_url(org.ws)).status_code in (401, 403)

    def test_deleted_workspace_is_404(self, admin_client, org):
        org.ws.deleted_at = timezone.now()
        org.ws.save(update_fields=["deleted_at"])
        resp = admin_client.get(_invitations_url(org.ws))
        assert resp.status_code == 404
        assert resp.json()["localizable_error"] == ERR_404_WORKSPACE_NOT_FOUND

    def test_list_is_anchor_paginated(self, admin_client, org):
        """stapel-core mandate: cursor windows, never limit/offset.

        An admin working through a long pending list while invitations are
        still going out must not have rows slip under the window.
        """
        for n in range(3):
            _invite(org.ws, f"extra{n}@example.com", org.owner)
        body = admin_client.get(_invitations_url(org.ws, limit=2)).json()
        assert len(body["items"]) == 2
        assert body["next_anchor"]


@pytest.mark.django_db
class TestFiltersAreTheCanonicalPredicates:
    """The wire vocabulary selects exactly what the library predicates select.

    The AST ban in ``test_lifecycle_predicates.py`` stops a *spelling* from
    being copied; it cannot notice a filter that calls the right method on
    the wrong set. These assertions are the second half: the endpoint's
    answer IS the predicate's answer, so a future change to what "pending"
    means moves the screen and the seat count together or not at all.
    """

    def _emails(self, client, ws, status):
        body = client.get(_invitations_url(ws, status=status)).json()
        return {i["email"] for i in body["items"]}

    def test_pending_filter_equals_pending_predicate(self, admin_client, org):
        assert self._emails(admin_client, org.ws, "pending") == {
            i.email for i in org.ws.invitations.pending()
        }

    def test_never_accepted_filter_equals_never_accepted_predicate(
        self, admin_client, org
    ):
        assert self._emails(admin_client, org.ws, "never_accepted") == {
            i.email for i in org.ws.invitations.never_accepted()
        }


# ---------------------------------------------------------------------------
# POST {ws}/invitations/{id}/revoke
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestRevoke:
    def _revoke(self, client, ws, inv):
        return client.post(f"{BASE}/{ws.id}/invitations/{inv.id}/revoke")

    def test_revoke_marks_the_row_and_answers_with_it(self, admin_client, org):
        resp = self._revoke(admin_client, org.ws, org.pending)
        assert resp.status_code == 200, resp.content
        assert resp.json()["status"] == InvitationStatus.REVOKED
        assert resp.json()["revoked_at"] is not None
        org.pending.refresh_from_db()
        assert org.pending.revoked_at is not None

    def test_revoked_invitation_leaves_the_pending_list(self, admin_client, org):
        self._revoke(admin_client, org.ws, org.pending)
        body = admin_client.get(_invitations_url(org.ws)).json()
        assert body["items"] == []

    def test_revoke_frees_the_seat(self, admin_client, org):
        """Revocation is worth money.

        A pending invitation reserves a paid seat (``member_seats_quantity``
        counts ``pending()``); an org that invited ten and had two answer
        must not keep paying for eight.
        """
        before = entitlements.member_seats_quantity(org.ws)
        self._revoke(admin_client, org.ws, org.pending)
        assert entitlements.member_seats_quantity(org.ws) == before - 1

    def test_revoked_token_can_no_longer_be_accepted(self, admin_client, org, db):
        """The point of revoking: the link in the invitee's mailbox dies."""
        from stapel_core.django.users.models import User

        invitee = User.objects.create_user(
            username="pending-invitee",
            email="pending@example.com",
            password="testpass-1234",
        )
        self._revoke(admin_client, org.ws, org.pending)
        admin_client.force_authenticate(user=invitee)
        resp = admin_client.post(
            f"{BASE}/invitations/accept",
            {"token": org.pending.token},
            format="json",
        )
        assert resp.status_code == 400
        assert resp.json()["localizable_error"] == ERR_400_INVITATION_REVOKED
        assert not WorkspaceMember.objects.filter(
            workspace=org.ws, user=invitee
        ).exists()

    def test_the_actor_is_readable_afterwards(self, admin_client, org, other_user):
        """Who withdrew it — answerable from the API, not only from a bus.

        Until 0.23 the actor existed ONLY inside the emitted event, so a
        workspace could show WHEN a permissioned action happened and never
        BY WHOM: an audit gap on exactly the kind of act an audit is for.
        The question is asked here the way a screen asks it — read the
        invitation back and look.
        """
        admin = WorkspaceMember.objects.create(
            workspace=org.ws,
            user=other_user,
            role=Role.ADMIN,
            accepted_at=timezone.now(),
        ).user
        admin_client.force_authenticate(user=admin)

        body = self._revoke(admin_client, org.ws, org.pending).json()
        assert body["revoked_by_id"] == str(admin.id)

        listed = admin_client.get(_invitations_url(org.ws, status="all")).json()
        row = next(
            i for i in listed["items"] if i["email"] == "pending@example.com"
        )
        assert row["revoked_by_id"] == str(admin.id)
        assert row["revoked_at"] is not None
        org.pending.refresh_from_db()
        assert org.pending.revoked_by_id == admin.id

    def test_an_unrevoked_invitation_names_nobody(self, admin_client, org):
        """The field is the answer to "who", not a default-filled column: a
        live invitation has no revoker and must not appear to have one."""
        body = admin_client.get(_invitations_url(org.ws)).json()
        assert body["items"][0]["revoked_by_id"] is None

    def test_the_actor_survives_their_account_being_deleted(
        self, admin_client, org, other_user
    ):
        """``SET_NULL``, like ``invited_by``: the record degrades to an
        honest "somebody, no longer known here" instead of taking the
        invitation's whole history down with the account."""
        WorkspaceMember.objects.create(
            workspace=org.ws,
            user=other_user,
            role=Role.ADMIN,
            accepted_at=timezone.now(),
        )
        admin_client.force_authenticate(user=other_user)
        self._revoke(admin_client, org.ws, org.pending)

        other_user.delete()
        org.pending.refresh_from_db()
        assert org.pending.revoked_by_id is None
        assert org.pending.revoked_at is not None

    def test_revoke_emits_who_did_it(self, admin_client, org, revocations):
        """The event keeps carrying the actor too.

        The persisted column answers "who withdrew this invitation"; the
        event tells everyone else it happened. Both, not either. The
        payload is validated against the committed contract in
        ``schemas/emits/``, like every other emit of this module.
        """
        self._revoke(admin_client, org.ws, org.pending)
        assert len(revocations) == 1
        payload = revocations[0].payload
        assert payload == {
            "workspace_id": str(org.ws.id),
            "invitation_id": str(org.pending.id),
            "role": Role.MEMBER,
            "revoked_by": str(org.owner.id),
        }
        jsonschema.validate(
            payload,
            json.loads(
                (
                    Path(stapel_workspaces.__file__).resolve().parent
                    / "schemas"
                    / "emits"
                    / "workspace.invitation_revoked.json"
                ).read_text()
            ),
            format_checker=jsonschema.FormatChecker(),
        )

    def test_revoke_event_carries_no_email(self, admin_client, org, revocations):
        """An invitation names a person who consented to nothing here.

        The event fans out to every subscriber in the deployment; the
        invitation UUID is enough for one that legitimately holds the row.
        """
        self._revoke(admin_client, org.ws, org.pending)
        assert "pending@example.com" not in str(revocations)

    def test_failed_revoke_emits_nothing(self, admin_client, org, revocations):
        """Transactional outbox: the event leaves iff the write commits."""
        self._revoke(admin_client, org.ws, org.declined)
        assert revocations == []

    @pytest.mark.parametrize(
        "which,error_key",
        [
            ("accepted", ERR_400_INVITATION_ALREADY_USED),
            ("declined", ERR_400_INVITATION_DECLINED),
            ("revoked", ERR_400_INVITATION_REVOKED),
            ("expired", ERR_400_INVITATION_EXPIRED),
        ],
    )
    def test_non_pending_states_get_their_own_key(
        self, admin_client, org, which, error_key
    ):
        """Each dead end names itself — a shrug would leave the admin guessing
        whether to re-invite, remove a member, or do nothing."""
        resp = self._revoke(admin_client, org.ws, getattr(org, which))
        assert resp.status_code == 400
        assert resp.json()["localizable_error"] == error_key

    def test_member_without_the_mandate_is_refused(self, api_client, org):
        WorkspaceMember.objects.create(
            workspace=org.ws,
            user=org.outsider,
            role=Role.MEMBER,
            accepted_at=timezone.now(),
        )
        api_client.force_authenticate(user=org.outsider)
        resp = self._revoke(api_client, org.ws, org.pending)
        assert resp.status_code == 403
        org.pending.refresh_from_db()
        assert org.pending.revoked_at is None


@pytest.mark.django_db
class TestRevokeIsCompareAndSet:
    """The transition re-reads the row under a lock; the view's check is a hint.

    0.10.0 fixed this race in one direction — a revocation committing
    between accept's state check and accept's row lock used to lose, and
    the invite was accepted anyway. Both transitions now compare-and-set on
    the same clock-free ``unresolved()``. This class drives the other
    direction: the accept commits inside revoke's window.
    """

    def test_accept_committed_in_the_window_wins(self, org, db):
        """The window, reproduced exactly: a stale read, then a live commit.

        ``stale`` is the row as the revoke view read it — pending, by every
        field it holds. The accept then commits. A revocation that trusted
        that snapshot would write ``revoked_at`` over an accepted
        invitation and leave a member whose invite says the org withdrew
        it. The lock's ``unresolved()`` re-read is what refuses instead.
        """
        from stapel_core.django.users.models import User

        invitee = User.objects.create_user(
            username="race-invitee",
            email="pending@example.com",
            password="testpass-1234",
        )
        stale = WorkspaceInvitation.objects.get(pk=org.pending.pk)

        services.accept_invitation(invitation=org.pending, user=invitee)

        assert stale.accepted_at is None, "the caller still believes it is pending"
        with pytest.raises(ValueError):
            services.revoke_invitation(invitation=stale, revoked_by=org.owner)

        org.pending.refresh_from_db()
        # The accept stands; the row is NOT both accepted and revoked.
        assert org.pending.accepted_at is not None
        assert org.pending.revoked_at is None
        assert WorkspaceMember.objects.filter(
            workspace=org.ws, user=invitee
        ).exists()

    def test_resend_loses_the_same_window(self, org, db):
        """Resend holds the same end of the same rope.

        A resend that lost this race would rotate the token of an already
        accepted invitation and mail a fresh sign-in link for an account
        that has already joined.
        """
        from stapel_core.django.users.models import User

        invitee = User.objects.create_user(
            username="race-invitee-2",
            email="pending@example.com",
            password="testpass-1234",
        )
        stale = WorkspaceInvitation.objects.get(pk=org.pending.pk)
        token_before = stale.token

        services.accept_invitation(invitation=org.pending, user=invitee)

        with pytest.raises(ValueError):
            services.resend_invitation(invitation=stale)
        org.pending.refresh_from_db()
        assert org.pending.token == token_before

    def test_view_reports_the_winner_not_a_500(self, admin_client, org, monkeypatch):
        """A lost race is a keyed 400 the admin can read, not a crash."""

        def always_lost(*, invitation, revoked_by):
            invitation.accepted_at = timezone.now()
            invitation.save(update_fields=["accepted_at"])
            raise ValueError("invitation is not pending")

        monkeypatch.setattr(services, "revoke_invitation", always_lost)
        resp = admin_client.post(
            f"{BASE}/{org.ws.id}/invitations/{org.pending.id}/revoke"
        )
        assert resp.status_code == 400
        assert resp.json()["localizable_error"] == ERR_400_INVITATION_ALREADY_USED


# ---------------------------------------------------------------------------
# POST {ws}/invitations/{id}/resend
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestResend:
    def _resend(self, client, ws, inv):
        return client.post(f"{BASE}/{ws.id}/invitations/{inv.id}/resend")

    def test_the_link_already_in_the_invitees_mailbox_keeps_working(
        self, admin_client, org, db
    ):
        """The reversal of the pre-0.23 rotation, stated as what a person does.

        The overwhelmingly common resend is "they say it never arrived" —
        and then the invitee finds the first letter and clicks THAT one.
        Rotating bought nothing (the second letter goes to the same
        mailbox as the first) and cost exactly this: a live invitation
        answering "not found" to the person holding it.
        """
        from stapel_core.django.users.models import User

        invitee = User.objects.create_user(
            username="resend-invitee",
            email="pending@example.com",
            password="testpass-1234",
        )
        old = _time_passes(org.pending).token
        resp = self._resend(admin_client, org.ws, org.pending)
        assert resp.status_code == 200, resp.content
        org.pending.refresh_from_db()
        assert org.pending.token == old
        assert old.encode() not in resp.content

        admin_client.force_authenticate(user=invitee)
        accepted = admin_client.post(
            f"{BASE}/invitations/accept", {"token": old}, format="json"
        )
        assert accepted.status_code == 200, accepted.content

    def test_a_deployment_can_still_ask_for_rotation(self, admin_client, org, db):
        """The switch, and what turning it on actually does.

        A strict org may want every resend to invalidate its predecessor.
        That is a deployment's decision, so it is a setting rather than a
        fork — and with it on, the old link dies exactly as it used to.
        """
        from stapel_core.django.users.models import User

        invitee = User.objects.create_user(
            username="rotate-invitee",
            email="pending@example.com",
            password="testpass-1234",
        )
        old = _time_passes(org.pending).token
        with override_settings(
            STAPEL_WORKSPACES={"INVITATION_ROTATE_TOKEN_ON_RESEND": True}
        ):
            assert self._resend(admin_client, org.ws, org.pending).status_code == 200
        org.pending.refresh_from_db()
        assert org.pending.token != old

        admin_client.force_authenticate(user=invitee)
        resp = admin_client.post(
            f"{BASE}/invitations/accept", {"token": old}, format="json"
        )
        assert resp.status_code == 404
        assert resp.json()["localizable_error"] == ERR_404_INVITATION_NOT_FOUND

    def test_resend_restarts_the_ttl(self, admin_client, org):
        old_expiry = _time_passes(org.pending).expires_at
        self._resend(admin_client, org.ws, org.pending)
        org.pending.refresh_from_db()
        assert org.pending.expires_at > old_expiry

    def test_expired_invitation_is_resendable(self, admin_client, org):
        """The main reason anyone resends.

        An expired row is a delivery failure, not a decision; the three
        stored terminal timestamps are the decisions, and they are what
        ``unresolved()`` excludes.
        """
        _time_passes(org.expired)
        resp = self._resend(admin_client, org.ws, org.expired)
        assert resp.status_code == 200, resp.content
        assert resp.json()["status"] == InvitationStatus.PENDING
        org.expired.refresh_from_db()
        assert org.expired.expires_at > timezone.now()

    def test_resend_mails_the_same_link_as_a_reminder(
        self, admin_client, org, monkeypatch
    ):
        """The letter is the reminder type — a resend says "you are being
        reminded", not "you are being invited" (notifications catalog
        >= 0.6.1) — and it carries the link the invitee already has."""
        sent = []
        monkeypatch.setattr(
            services,
            "_send_invitation_notification",
            lambda inv, **kw: sent.append((inv, kw.get("notification_type"))),
        )
        _time_passes(org.pending)
        self._resend(admin_client, org.ws, org.pending)
        org.pending.refresh_from_db()
        assert [(i.token, t) for i, t in sent] == [
            (org.pending.token, "workspace.invitation.reminder")
        ]

    @pytest.mark.parametrize(
        "which,error_key",
        [
            ("accepted", ERR_400_INVITATION_ALREADY_USED),
            ("declined", ERR_400_INVITATION_DECLINED),
            ("revoked", ERR_400_INVITATION_REVOKED),
        ],
    )
    def test_decisions_are_not_resendable(
        self, admin_client, org, which, error_key
    ):
        inv = getattr(org, which)
        before = inv.token
        resp = self._resend(admin_client, org.ws, inv)
        assert resp.status_code == 400
        assert resp.json()["localizable_error"] == error_key
        inv.refresh_from_db()
        assert inv.token == before

    def test_member_without_the_mandate_is_refused(self, api_client, org):
        WorkspaceMember.objects.create(
            workspace=org.ws,
            user=org.outsider,
            role=Role.MEMBER,
            accepted_at=timezone.now(),
        )
        api_client.force_authenticate(user=org.outsider)
        before = org.pending.token
        assert self._resend(api_client, org.ws, org.pending).status_code == 403
        org.pending.refresh_from_db()
        assert org.pending.token == before


# ---------------------------------------------------------------------------
# The resend cooldown: how often may we mail the same person?
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestResendCooldown:
    """Resend used to have no limit of any kind.

    Not a rate limit that was too generous — none at all, and no record of
    the previous send to build one from. One admin holding `members.invite`
    could sit on the endpoint and drive an unbounded number of letters at
    one address through this fleet's mail infrastructure; with the old
    unconditional rotation, each pass also churned the credential.

    The window belongs to the invited ADDRESS, not to the calling admin —
    ten admins are ten DRF throttle buckets and one mailbox — so these
    tests are written from the mailbox's side.
    """

    def _resend(self, client, ws, inv):
        return client.post(f"{BASE}/{ws.id}/invitations/{inv.id}/resend")

    def test_the_second_send_inside_the_window_is_refused(self, admin_client, org):
        _time_passes(org.pending)
        assert self._resend(admin_client, org.ws, org.pending).status_code == 200

        again = self._resend(admin_client, org.ws, org.pending)
        assert again.status_code == 429
        body = again.json()
        assert body["localizable_error"] == ERR_429_INVITATION_RESEND_COOLDOWN
        assert 0 < body["params"]["retry_after"] <= 600
        assert again["Retry-After"] == str(body["params"]["retry_after"])

    def test_the_refusal_sends_no_letter(self, admin_client, org, monkeypatch):
        """The whole point: refusing has to mean the mail does not go out."""
        sent = []
        monkeypatch.setattr(
            services,
            "_send_invitation_notification",
            lambda inv, **kw: sent.append(kw.get("notification_type")),
        )
        _time_passes(org.pending)
        self._resend(admin_client, org.ws, org.pending)
        for _ in range(5):
            assert self._resend(admin_client, org.ws, org.pending).status_code == 429
        assert sent == ["workspace.invitation.reminder"]

    def test_the_refusal_changes_nothing_on_the_row(self, admin_client, org):
        """A refused resend is a no-op, not a partial one: the TTL is not
        restarted and the token is not touched."""
        _time_passes(org.pending)
        self._resend(admin_client, org.ws, org.pending)
        org.pending.refresh_from_db()
        token, expiry = org.pending.token, org.pending.expires_at

        assert self._resend(admin_client, org.ws, org.pending).status_code == 429
        org.pending.refresh_from_db()
        assert (org.pending.token, org.pending.expires_at) == (token, expiry)

    def test_the_letter_that_created_the_invitation_starts_the_clock(
        self, admin_client, org
    ):
        """An invitation was just mailed. Resending it one second later
        mails the same person the same thing twice — the clock counts
        letters, not resends."""
        assert org.pending.last_sent_at is not None
        resp = self._resend(admin_client, org.ws, org.pending)
        assert resp.status_code == 429

    def test_after_the_window_it_goes_through(self, admin_client, org):
        _time_passes(org.pending, seconds=601)
        assert self._resend(admin_client, org.ws, org.pending).status_code == 200

    def test_the_window_is_the_addresss_not_the_rows(self, admin_client, org):
        """Re-inviting the same address makes a NEW row — and a per-row
        clock would start it empty, handing the loop back its letters."""
        _time_passes(org.pending, seconds=601)
        assert self._resend(admin_client, org.ws, org.pending).status_code == 200

        twin = _invite(org.ws, "pending@example.com", org.owner)
        twin.last_sent_at = None
        twin.save(update_fields=["last_sent_at"])
        assert self._resend(admin_client, org.ws, twin).status_code == 429

    def test_a_deployment_can_choose_the_window(self, admin_client, org):
        _time_passes(org.pending, seconds=601)
        with override_settings(
            STAPEL_WORKSPACES={"INVITATION_RESEND_COOLDOWN_SECONDS": 30}
        ):
            assert self._resend(admin_client, org.ws, org.pending).status_code == 200
            _time_passes(org.pending, seconds=31)
            assert self._resend(admin_client, org.ws, org.pending).status_code == 200

    def test_a_deployment_can_switch_it_off(self, admin_client, org):
        with override_settings(
            STAPEL_WORKSPACES={"INVITATION_RESEND_COOLDOWN_SECONDS": 0}
        ):
            for _ in range(3):
                assert (
                    self._resend(admin_client, org.ws, org.pending).status_code == 200
                )

    def test_an_environment_variable_configures_it_too(self, admin_client, org):
        """AppSettings hands env values back as raw strings and coerces
        nothing; a limiter that quietly read "600" as garbage (and so as
        "off") is the exact failure this key must not have."""
        _time_passes(org.pending, seconds=601)
        with override_settings(
            STAPEL_WORKSPACES={"INVITATION_RESEND_COOLDOWN_SECONDS": "600"}
        ):
            assert self._resend(admin_client, org.ws, org.pending).status_code == 200
            assert self._resend(admin_client, org.ws, org.pending).status_code == 429

    def test_a_malformed_window_is_a_deploy_error_not_a_silent_off_switch(self):
        """``"10m"`` is the shape of the neighbouring INVITATION_THROTTLE
        and is meaningless here. A deployment that writes it must find out
        at ``manage.py check`` rather than from a mailbox."""
        from stapel_workspaces.checks import check_invitation_resend_cooldown

        with override_settings(
            STAPEL_WORKSPACES={"INVITATION_RESEND_COOLDOWN_SECONDS": "10m"}
        ):
            errors = check_invitation_resend_cooldown(None)
        assert [e.id for e in errors] == ["stapel_workspaces.E009"]

    def test_an_invitation_from_before_the_column_is_resendable(
        self, admin_client, org
    ):
        """NULL means "no letter recorded", which owes no cooldown — rows
        that predate the migration must not be frozen by it."""
        org.pending.last_sent_at = None
        org.pending.save(update_fields=["last_sent_at"])
        assert self._resend(admin_client, org.ws, org.pending).status_code == 200

    def test_the_clock_is_visible_to_the_client(self, admin_client, org):
        """A screen that can see the last send can disable its own resend
        button instead of teaching the admin about the limit with a 429."""
        row = admin_client.get(_invitations_url(org.ws)).json()["items"][0]
        assert row["last_sent_at"] is not None

    def test_a_letter_that_never_reached_the_mailer_spends_the_window(
        self, admin_client, org, monkeypatch
    ):
        """Fail-closed, deliberately.

        The send happens after the row lock commits, so the window has to
        be claimed under the lock — before anyone knows whether the mailer
        took the letter. The alternative (claim it only on success) leaves
        two simultaneous presses both passing a check neither has written
        to yet, which is the loop this exists to stop.
        """
        _time_passes(org.pending)
        monkeypatch.setattr(
            services,
            "_send_invitation_notification",
            lambda inv, **kw: (_ for _ in ()).throw(RuntimeError("mailer down")),
        )
        with pytest.raises(RuntimeError):
            self._resend(admin_client, org.ws, org.pending)
        monkeypatch.undo()
        assert self._resend(admin_client, org.ws, org.pending).status_code == 429


# ---------------------------------------------------------------------------
# Cross-workspace scoping: the same answer for "gone" and "not yours"
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestInvitationIdIsScopedToTheWorkspace:
    """An admin of one org must not learn anything about another org's rows.

    The invitation id is a UUID in a URL an admin controls both halves of.
    A "not found" for an unknown id and a "forbidden" for a real id in
    somebody else's workspace would together turn the endpoint into an
    existence oracle for invitation ids.
    """

    @pytest.fixture
    def foreign_invitation(self, org, other_user, db):
        their_ws = services.create_workspace(user=other_user, name="Other")
        return _invite(their_ws, "theirs@example.com", other_user)

    @pytest.mark.parametrize("action", ["revoke", "resend"])
    def test_foreign_and_unknown_are_indistinguishable(
        self, admin_client, org, foreign_invitation, action
    ):
        unknown = "44444444-4444-4444-4444-444444444444"
        foreign = admin_client.post(
            f"{BASE}/{org.ws.id}/invitations/{foreign_invitation.id}/{action}"
        )
        missing = admin_client.post(
            f"{BASE}/{org.ws.id}/invitations/{unknown}/{action}"
        )
        assert foreign.status_code == missing.status_code == 404
        assert foreign.content == missing.content
        assert foreign.json()["localizable_error"] == ERR_404_INVITATION_NOT_FOUND

    def test_foreign_invitation_is_untouched(
        self, admin_client, org, foreign_invitation
    ):
        before = foreign_invitation.token
        admin_client.post(
            f"{BASE}/{org.ws.id}/invitations/{foreign_invitation.id}/revoke"
        )
        admin_client.post(
            f"{BASE}/{org.ws.id}/invitations/{foreign_invitation.id}/resend"
        )
        foreign_invitation.refresh_from_db()
        assert foreign_invitation.revoked_at is None
        assert foreign_invitation.token == before

    def test_list_does_not_leak_across_workspaces(
        self, admin_client, org, foreign_invitation
    ):
        body = admin_client.get(_invitations_url(org.ws, status="all")).json()
        assert "theirs@example.com" not in {i["email"] for i in body["items"]}
