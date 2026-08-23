"""Subject-scoped erasure and the receipt that proves it happened.

Before this, the module erased an account's slice on ``user.deleted`` and
said nothing. stapel-gdpr's orchestrator does not self-certify: an
``ErasurePart`` with no receipt keeps the request in ``erasing`` until it
times out thirty days later, so a silent owner is indistinguishable from an
owner whose consumer was never deployed. The pins here are, in order:

* the workspace subject exists at all (memberships, invitations, the MFA
  enforcement record, the provisioning saga rows, and the row itself);
* every erasure answers with ``gdpr.section.erased`` carrying **counts** —
  "it ran" and "it removed 4 memberships and 2 invitations" are different
  claims, and only the second can be audited;
* a redelivery erases nothing and still receipts (at-least-once delivery
  means the second copy must not leave the request unconfirmed);
* the probe is answered **from this same module**, which is the only reason
  ``gdpr.owner.alive`` is evidence about the erasure path rather than about
  a running container.
"""

import json
import types
import uuid
from pathlib import Path

import jsonschema
import pytest
from django.utils import timezone

from stapel_core.comm import subscribe_action
from stapel_workspaces.actions import (
    handle_erasure_requested,
    handle_owner_probe,
    handle_user_deleted,
)
from stapel_workspaces.erasure import GDPR_OWNER, GDPR_SUBJECT_TYPES, erase_workspace
from stapel_workspaces.models import (
    Role,
    Workspace,
    WorkspaceInvitation,
    WorkspaceMember,
    WorkspaceMFAEnforcement,
    WorkspaceProvisionOperation,
)

SCHEMAS = Path(__file__).resolve().parent.parent / "schemas"


def _validate(payload: dict, name: str) -> None:
    jsonschema.validate(
        payload,
        json.loads((SCHEMAS / "emits" / f"{name}.json").read_text()),
        format_checker=jsonschema.FormatChecker(),
    )


def _request(subject_type, subject_key, correlation_id=None):
    return types.SimpleNamespace(
        payload={
            "correlation_id": str(correlation_id or uuid.uuid4()),
            "subject_type": subject_type,
            "subject_key": str(subject_key),
        },
        event_id="e1",
        service="gdpr",
    )


@pytest.fixture
def receipts():
    events = []
    subscribe_action("gdpr.section.erased", events.append)
    return events


@pytest.fixture
def alive():
    events = []
    subscribe_action("gdpr.owner.alive", events.append)
    return events


def _create_ws(user, name="Acme"):
    from stapel_workspaces.services import create_workspace

    return create_workspace(user=user, name=name)


def _populate(ws, user, other_user):
    """Give the workspace one of everything this module owns about it."""
    WorkspaceMember.objects.create(
        workspace=ws, user=other_user, role=Role.MEMBER, accepted_at=timezone.now()
    )
    from stapel_workspaces.services import create_invitation

    create_invitation(
        workspace=ws, email="pending@example.com", role=Role.MEMBER, invited_by=user
    )
    WorkspaceMFAEnforcement.objects.create(workspace=ws)
    WorkspaceProvisionOperation.objects.create(
        workspace=ws, operation_id="op-1", username="provisioned", user_id=uuid.uuid4()
    )


@pytest.mark.django_db
class TestWorkspaceErasure:
    """The purge window is the request: when it arrives, the workspace goes."""

    def test_everything_scoped_to_the_workspace_is_removed(
        self, user, other_user
    ):
        ws = _create_ws(user)
        _populate(ws, user, other_user)

        counts = erase_workspace(ws.id)

        assert not Workspace.objects.filter(id=ws.id).exists()
        assert not WorkspaceMember.objects.filter(workspace_id=ws.id).exists()
        assert not WorkspaceInvitation.objects.filter(workspace_id=ws.id).exists()
        assert not WorkspaceMFAEnforcement.objects.filter(
            workspace_id=ws.id
        ).exists()
        assert not WorkspaceProvisionOperation.objects.filter(
            workspace_id=ws.id
        ).exists()
        assert counts == {
            "memberships": 2,
            "invitations": 1,
            "mfa_enforcements": 1,
            "provision_operations": 1,
            "workspaces": 1,
        }

    def test_a_tombstone_is_not_kept_after_the_window(self, user):
        """``delete_workspace`` keeps the row so peers can resolve the id
        while they clean up. The erasure request says that time is over —
        name, slug, settings and the owner link go with it."""
        ws = _create_ws(user)
        ws.deleted_at = timezone.now()
        ws.save(update_fields=["deleted_at"])

        erase_workspace(ws.id)

        assert Workspace.objects.count() == 0

    def test_another_workspace_is_untouched(self, user, other_user):
        doomed = _create_ws(user, name="Doomed")
        kept = _create_ws(other_user, name="Kept")

        erase_workspace(doomed.id)

        assert Workspace.objects.filter(id=kept.id).exists()
        assert WorkspaceMember.objects.filter(workspace_id=kept.id).count() == 1


@pytest.mark.django_db
class TestTheReceipt:
    def test_workspace_erasure_receipts_with_counts(
        self, user, other_user, receipts
    ):
        ws = _create_ws(user)
        _populate(ws, user, other_user)
        correlation = uuid.uuid4()

        handle_erasure_requested(_request("workspace", ws.id, correlation))

        assert len(receipts) == 1
        payload = receipts[0].payload
        assert payload["correlation_id"] == str(correlation)
        assert payload["owner"] == GDPR_OWNER
        assert payload["subject_type"] == "workspace"
        assert payload["subject_key"] == str(ws.id)
        assert payload["counts"]["memberships"] == 2
        assert payload["counts"]["workspaces"] == 1
        _validate(payload, "gdpr.section.erased")

    def test_account_erasure_receipts_with_counts(self, user, receipts):
        _create_ws(user)

        handle_erasure_requested(_request("account", user.pk))

        assert len(receipts) == 1
        payload = receipts[0].payload
        assert payload["subject_type"] == "account"
        assert payload["counts"]["memberships"] == 1
        assert payload["counts"]["workspaces_soft_deleted"] == 1
        _validate(payload, "gdpr.section.erased")
        assert not WorkspaceMember.objects.filter(user=user).exists()

    def test_a_redelivery_erases_nothing_and_still_receipts(
        self, user, other_user, receipts
    ):
        """At-least-once delivery: the second copy must not leave the
        orchestrator's part unconfirmed, and must not claim a second
        erasure either."""
        ws = _create_ws(user)
        _populate(ws, user, other_user)
        event = _request("workspace", ws.id)

        handle_erasure_requested(event)
        handle_erasure_requested(event)

        assert len(receipts) == 2
        assert receipts[1].payload["counts"] == {
            "memberships": 0,
            "invitations": 0,
            "mfa_enforcements": 0,
            "provision_operations": 0,
            "workspaces": 0,
        }

    def test_a_subject_type_we_do_not_claim_gets_no_receipt(self, user, receipts):
        """A receipt from an owner that erased nothing is worse than
        silence — the orchestrator counts it and finalizes."""
        handle_erasure_requested(_request("recording", uuid.uuid4()))

        assert receipts == []

    def test_an_unusable_key_gets_no_receipt(self, user, receipts, caplog):
        handle_erasure_requested(_request("workspace", "not-a-uuid"))

        assert receipts == []
        assert "unusable" in caplog.text

    def test_a_malformed_request_gets_no_receipt(self, receipts, caplog):
        handle_erasure_requested(
            types.SimpleNamespace(
                payload={"subject_type": "workspace"}, event_id="e9", service="gdpr"
            )
        )

        assert receipts == []


@pytest.mark.django_db
class TestDeprecatedUserDeletedPath:
    """``user.deleted`` fires alongside the new event until gdpr 0.6.0."""

    def test_it_erases_through_the_same_code(self, user):
        _create_ws(user)
        handle_user_deleted(
            types.SimpleNamespace(payload={"user_id": user.pk}, event_id="e1")
        )
        assert not WorkspaceMember.objects.filter(user=user).exists()

    def test_it_receipts_when_the_event_carries_a_correlation(
        self, user, receipts
    ):
        """The silent-owner finding: this handler erased and said nothing,
        so a host still on the account-only protocol timed out."""
        _create_ws(user)
        correlation = uuid.uuid4()

        handle_user_deleted(
            types.SimpleNamespace(
                payload={"user_id": str(user.pk), "correlation_id": str(correlation)},
                event_id="e1",
            )
        )

        assert len(receipts) == 1
        assert receipts[0].payload["correlation_id"] == str(correlation)
        assert receipts[0].payload["subject_type"] == "account"
        _validate(receipts[0].payload, "gdpr.section.erased")

    def test_without_a_correlation_it_erases_and_stays_quiet(self, user, receipts):
        _create_ws(user)
        handle_user_deleted(
            types.SimpleNamespace(payload={"user_id": user.pk}, event_id="e1")
        )
        assert receipts == []


@pytest.mark.django_db
class TestTheProbe:
    def test_it_answers_with_owner_and_subject_types(self, alive):
        handle_owner_probe(
            types.SimpleNamespace(
                payload={"correlation_id": str(uuid.uuid4())},
                event_id="p1",
                service="gdpr",
            )
        )

        assert len(alive) == 1
        payload = alive[0].payload
        assert payload["owner"] == GDPR_OWNER
        assert payload["subject_types"] == list(GDPR_SUBJECT_TYPES)
        _validate(payload, "gdpr.owner.alive")

    def test_the_claimed_types_are_the_ones_the_eraser_handles(self):
        from stapel_workspaces.erasure import ERASERS

        assert set(GDPR_SUBJECT_TYPES) == set(ERASERS)

    def test_it_is_answered_from_the_erasure_subscriber(self):
        """Co-location IS the contract: answering the probe from anywhere
        else would make ``alive`` a statement about a deployed container
        rather than about a consumed erasure path."""
        assert (
            handle_owner_probe.__module__
            == handle_erasure_requested.__module__
            == "stapel_workspaces.actions"
        )

    def test_the_owner_name_is_the_providers_section(self):
        from stapel_workspaces.gdpr import WorkspacesGDPRProvider

        assert WorkspacesGDPRProvider.section == GDPR_OWNER


@pytest.mark.django_db
class TestAccountErasureDetails:
    def test_the_provisioning_saga_keeps_the_money_and_loses_the_person(
        self, user
    ):
        ws = _create_ws(user)
        op = WorkspaceProvisionOperation.objects.create(
            workspace=ws,
            operation_id="op-1",
            username="someone",
            user_id=user.pk,
            credits_to_refund=5,
        )

        from stapel_workspaces.erasure import erase_account

        counts = erase_account(user.pk)

        op.refresh_from_db()
        assert op.username == ""
        assert op.user_id is None
        assert op.credits_to_refund == 5
        assert counts["provision_operations_anonymized"] == 1

    def test_owned_workspaces_are_soft_deleted_not_destroyed(self, user):
        """Others may still be members, and other modules still hold data
        keyed by this id — whether the workspace itself dies is a workspace
        erasure, not a side effect of its owner leaving."""
        ws = _create_ws(user)

        from stapel_workspaces.erasure import erase_account

        erase_account(user.pk)

        ws.refresh_from_db()
        assert ws.deleted_at is not None
