"""Deleting a workspace — the terminal state, its announcement, its refusals.

Deletion was never missing: ``WorkspaceDetailView.delete`` has soft-deleted
via ``deleted_at`` since early on. Everything AROUND it was missing, and each
gap is one of the pins below.

* **Nobody learned.** A workspace ended and no event said so, while the
  fleet's whole peer-notification mechanism is the event store. Recordings,
  docs, calendar and tasks all key data by workspace id and had no way to
  hear that the id had died.
* **Its own history did not contain its death.** ``record_audit`` is called
  from every other transition in this module and was not called from this
  one, so the roster's journal ended mid-sentence.
* **The transition lived in the view**, against this module's own stated
  rule (``audit.py``: THE ONE WRITE PATH, called from the service that owns
  the transition) — a second door into a transition is a second chance to
  forget the record.
* **One refusal that said nothing**, and no guard at all on the one deletion
  that can lock an instance: its DEFAULT workspace, whose owners ARE
  ``instance_owner_ids()``. Deleting it empties the instance's owner set.

The refusals say WHY. "You cannot" without "because" is the defect this
whole pass exists to close, and a screen cannot render a reason it was
never given.
"""
import json
import uuid
from pathlib import Path

import jsonschema
import pytest
from django.test import override_settings
from django.utils import timezone

from stapel_core import eventstore
from stapel_core.comm import subscribe_action
from stapel_workspaces.errors import (
    ERR_409_WORKSPACE_IS_INSTANCE_DEFAULT,
    ERR_409_WORKSPACE_IS_PERSONAL,
)
from stapel_workspaces.events import EVENT_WORKSPACE_DELETED
from stapel_workspaces.models import AuditAction, Role, Workspace, WorkspaceMember, WorkspaceType
from stapel_workspaces.services import (
    WorkspaceDeletionRefused,
    create_workspace,
    delete_workspace,
    deletion_block_reason,
    instance_owner_ids,
)

BASE = "/workspaces/api/workspaces/v1"
AUDIT_STREAM = "workspace.audit"


def _audit_actions(workspace):
    return [
        e.payload["action"]
        for e in eventstore.query(
            AUDIT_STREAM, filters={"workspace_id": str(workspace.id)}, limit=500
        )
    ]


@pytest.mark.django_db
class TestDeletionHappyPath:
    def test_owner_deletes_and_the_row_reaches_its_terminal_state(
        self, authed_client, user
    ):
        ws = create_workspace(user=user, name="Doomed", type=WorkspaceType.WORK)
        assert authed_client.delete(f"{BASE}/{ws.id}").status_code == 204
        ws.refresh_from_db()
        assert ws.deleted_at is not None

    def test_the_row_survives_as_a_tombstone(self, authed_client, user):
        """Terminal state, not row removal — the audit stream and every peer
        that stored this id must keep resolving it to something."""
        ws = create_workspace(user=user, name="Doomed", type=WorkspaceType.WORK)
        authed_client.delete(f"{BASE}/{ws.id}")
        assert Workspace.objects.filter(id=ws.id).exists()

    def test_the_workspace_leaves_the_callers_list(self, authed_client, user):
        ws = create_workspace(user=user, name="Doomed", type=WorkspaceType.WORK)
        authed_client.delete(f"{BASE}/{ws.id}")
        listed = authed_client.get(f"{BASE}/").json()["workspaces"]
        assert str(ws.id) not in {w["id"] for w in listed}

    def test_a_deleted_workspace_is_gone_for_reads(self, authed_client, user):
        ws = create_workspace(user=user, name="Doomed", type=WorkspaceType.WORK)
        authed_client.delete(f"{BASE}/{ws.id}")
        assert authed_client.get(f"{BASE}/{ws.id}").status_code == 404

    def test_deleting_twice_is_a_404_not_a_second_deletion(self, authed_client, user):
        ws = create_workspace(user=user, name="Doomed", type=WorkspaceType.WORK)
        authed_client.delete(f"{BASE}/{ws.id}")
        assert authed_client.delete(f"{BASE}/{ws.id}").status_code == 404


@pytest.fixture
def deletions():
    """Collect ``workspace.deleted`` payloads off the comm bus."""
    events = []
    subscribe_action(EVENT_WORKSPACE_DELETED, events.append)
    return events


@pytest.mark.django_db
class TestDeletionIsAnnounced:
    """A deletion peers must observe is an event, not a silent row update."""

    def test_it_emits_workspace_deleted(self, user, deletions):
        ws = create_workspace(user=user, name="Doomed", type=WorkspaceType.WORK)
        delete_workspace(workspace=ws, actor=user)
        assert len(deletions) == 1

    def test_the_payload_carries_what_a_peer_needs_to_act(self, user, deletions):
        ws = create_workspace(user=user, name="Doomed", type=WorkspaceType.WORK)
        delete_workspace(workspace=ws, actor=user)
        payload = deletions[0].payload
        assert payload["workspace_id"] == str(ws.id)
        assert payload["owner_id"] == str(user.pk)
        assert payload["deleted_by"] == str(user.pk)
        assert payload["type"] == WorkspaceType.WORK
        assert payload["member_count"] == 1

    def test_the_payload_matches_its_published_schema(self, user, deletions):
        """The emit contract is a file in schemas/emits/, like every other
        emit of this module — a peer integrates against that, not against
        whatever the producer happened to send."""
        ws = create_workspace(user=user, name="Doomed", type=WorkspaceType.WORK)
        delete_workspace(workspace=ws, actor=user)
        schema = json.loads(
            (
                Path(__file__).resolve().parent.parent
                / "schemas"
                / "emits"
                / "workspace.deleted.json"
            ).read_text()
        )
        jsonschema.validate(
            deletions[0].payload, schema, format_checker=jsonschema.FormatChecker()
        )

    def test_the_payload_carries_no_email(self, user, deletions):
        """Same rule the password-reset event states for itself: the event
        fans out to every subscriber, so it carries ids and counts only."""
        ws = create_workspace(user=user, name="Doomed", type=WorkspaceType.WORK)
        delete_workspace(workspace=ws, actor=user)
        assert "@" not in repr(deletions[0].payload)

    def test_a_refused_deletion_emits_nothing(self, user, deletions):
        ws = create_workspace(user=user, name="Home", type=WorkspaceType.WORK)
        with override_settings(
            STAPEL_WORKSPACES={"DEFAULT_WORKSPACE_ID": str(ws.id)}
        ):
            with pytest.raises(WorkspaceDeletionRefused):
                delete_workspace(workspace=ws, actor=user)
        assert deletions == []

    def test_the_workspaces_own_history_records_its_death(self, user):
        ws = create_workspace(user=user, name="Doomed", type=WorkspaceType.WORK)
        delete_workspace(workspace=ws, actor=user)
        assert AuditAction.DELETED in _audit_actions(ws)


@pytest.mark.django_db
class TestRefusalsSayWhy:
    def test_a_non_owner_is_refused(self, api_client, user, other_user):
        ws = create_workspace(user=other_user, name="Theirs", type=WorkspaceType.WORK)
        WorkspaceMember.objects.create(
            workspace=ws, user=user, role=Role.ADMIN, accepted_at=timezone.now()
        )
        api_client.force_authenticate(user=user)
        resp = api_client.delete(f"{BASE}/{ws.id}")
        assert resp.status_code == 403
        ws.refresh_from_db()
        assert ws.deleted_at is None

    def test_the_instance_default_cannot_be_deleted(self, authed_client, user):
        """The lockout guard. Its owners ARE instance_owner_ids(); deleting it
        would leave an instance_owner-policy instance with nobody who may
        ever found a workspace again."""
        ws = create_workspace(user=user, name="Home", type=WorkspaceType.WORK)
        with override_settings(
            STAPEL_WORKSPACES={"DEFAULT_WORKSPACE_ID": str(ws.id)}
        ):
            resp = authed_client.delete(f"{BASE}/{ws.id}")
        assert resp.status_code == 409
        assert resp.json()["localizable_error"] == ERR_409_WORKSPACE_IS_INSTANCE_DEFAULT
        ws.refresh_from_db()
        assert ws.deleted_at is None

    def test_the_instance_keeps_its_owners_after_the_refusal(self, authed_client, user):
        ws = create_workspace(user=user, name="Home", type=WorkspaceType.WORK)
        with override_settings(
            STAPEL_WORKSPACES={"DEFAULT_WORKSPACE_ID": str(ws.id)}
        ):
            authed_client.delete(f"{BASE}/{ws.id}")
            assert instance_owner_ids() == {user.pk}

    def test_a_personal_workspace_that_would_be_reminted_is_refused(
        self, authed_client, user
    ):
        """Deleting it does not remove it — ensure_personal_workspace filters
        deleted_at, so the next landing mints a NEW one with a NEW id. That is
        identity churn wearing a deletion's clothes."""
        ws = create_workspace(user=user, name="Personal", type=WorkspaceType.PERSONAL)
        with override_settings(
            STAPEL_WORKSPACES={"STREET_LANDING_MODE": "personal"}
        ):
            resp = authed_client.delete(f"{BASE}/{ws.id}")
        assert resp.status_code == 409
        assert resp.json()["localizable_error"] == ERR_409_WORKSPACE_IS_PERSONAL
        ws.refresh_from_db()
        assert ws.deleted_at is None

    def test_a_personal_workspace_is_deletable_when_nothing_remints_it(
        self, authed_client, user
    ):
        """The refusal is conditional on the mode, because under "none" its
        stated reason would simply be false."""
        ws = create_workspace(user=user, name="Personal", type=WorkspaceType.PERSONAL)
        with override_settings(STAPEL_WORKSPACES={"STREET_LANDING_MODE": "none"}):
            assert authed_client.delete(f"{BASE}/{ws.id}").status_code == 204

    def test_an_unknown_workspace_is_a_404(self, authed_client):
        assert authed_client.delete(f"{BASE}/{uuid.uuid4()}").status_code == 404

    def test_the_service_raises_a_refusal_carrying_its_code(self, user):
        ws = create_workspace(user=user, name="Home", type=WorkspaceType.WORK)
        with override_settings(
            STAPEL_WORKSPACES={"DEFAULT_WORKSPACE_ID": str(ws.id)}
        ):
            with pytest.raises(WorkspaceDeletionRefused) as excinfo:
                delete_workspace(workspace=ws, actor=user)
        assert excinfo.value.code == ERR_409_WORKSPACE_IS_INSTANCE_DEFAULT


@pytest.mark.django_db
class TestTheScreenIsToldBeforeItDraws:
    """A button that leads to a refusal is the defect. The detail response
    answers "may this caller delete this, and if not why" so the danger zone
    renders the reason instead of discovering it on click."""

    def test_an_owner_of_an_ordinary_workspace_may_delete(self, authed_client, user):
        ws = create_workspace(user=user, name="Ordinary", type=WorkspaceType.WORK)
        body = authed_client.get(f"{BASE}/{ws.id}").json()
        assert body["can_delete"] is True
        assert body["delete_blocked_reason"] == ""

    def test_an_admin_is_told_it_is_not_theirs_to_delete(
        self, api_client, user, other_user
    ):
        ws = create_workspace(user=other_user, name="Theirs", type=WorkspaceType.WORK)
        WorkspaceMember.objects.create(
            workspace=ws, user=user, role=Role.ADMIN, accepted_at=timezone.now()
        )
        api_client.force_authenticate(user=user)
        body = api_client.get(f"{BASE}/{ws.id}").json()
        assert body["can_delete"] is False
        assert body["delete_blocked_reason"] != ""

    def test_the_instance_default_reports_its_reason_before_the_click(
        self, authed_client, user
    ):
        ws = create_workspace(user=user, name="Home", type=WorkspaceType.WORK)
        with override_settings(
            STAPEL_WORKSPACES={"DEFAULT_WORKSPACE_ID": str(ws.id)}
        ):
            body = authed_client.get(f"{BASE}/{ws.id}").json()
        assert body["can_delete"] is False
        assert body["delete_blocked_reason"] == ERR_409_WORKSPACE_IS_INSTANCE_DEFAULT

    def test_the_advertised_reason_is_the_one_the_delete_actually_returns(
        self, authed_client, user
    ):
        """THE GATE. The screen's reason and the endpoint's refusal are one
        evaluation, so they cannot drift into a button that promises one
        thing and a server that says another."""
        ws = create_workspace(user=user, name="Personal", type=WorkspaceType.PERSONAL)
        with override_settings(
            STAPEL_WORKSPACES={"STREET_LANDING_MODE": "personal"}
        ):
            advertised = authed_client.get(f"{BASE}/{ws.id}").json()[
                "delete_blocked_reason"
            ]
            actual = authed_client.delete(f"{BASE}/{ws.id}").json()["localizable_error"]
        assert advertised == actual

    def test_deletion_block_reason_is_empty_for_an_ordinary_workspace(self, user):
        ws = create_workspace(user=user, name="Ordinary", type=WorkspaceType.WORK)
        assert deletion_block_reason(ws) == ""
