"""Workspace.storage_used_bytes follows document.storage_changed exactly once per event."""
import uuid
from datetime import timedelta
from io import StringIO

import pytest
from django.core.management import call_command
from django.utils import timezone

from stapel_core.bus.event import Event
from stapel_core.comm.actions import deliver
from stapel_core.comm.registry import function_registry

from stapel_workspaces.models import StorageDeltaReceipt, Workspace
from stapel_workspaces.services import create_workspace
from stapel_workspaces.storage_usage import (
    STORAGE_CHANGED,
    apply_storage_delta,
    prune_receipts,
    reconcile_workspace,
)

pytestmark = pytest.mark.django_db


def _event(workspace_id, delta, event_id=None):
    return Event(
        event_type=STORAGE_CHANGED,
        service="docs",
        payload={"workspace_id": str(workspace_id), "delta_bytes": delta},
        event_id=event_id or str(uuid.uuid4()),
    )


def _used(ws):
    return Workspace.objects.get(pk=ws.pk).storage_used_bytes


@pytest.fixture
def ws(user):
    return create_workspace(user=user, name="Acme")


@pytest.fixture
def docs_usage():
    """A storage owner answering ``docs.usage`` with a settable figure."""
    figures = {}
    seen = []

    def provider(payload):
        seen.append(payload)
        if payload["workspace_id"] not in figures:
            raise LookupError("unknown workspace")
        return {"bytes_total": figures[payload["workspace_id"]]}

    function_registry.register("docs.usage", provider)
    yield type("U", (), {"figures": figures, "seen": seen})()
    function_registry._providers.pop("docs.usage", None)


def test_deltas_move_the_counter_through_the_bus(ws):
    deliver(_event(ws.pk, 1000))
    deliver(_event(ws.pk, 500))
    deliver(_event(ws.pk, -300))
    assert _used(ws) == 1200


def test_a_redelivery_is_a_no_op(ws):
    event = _event(ws.pk, 4096)
    deliver(event)
    deliver(event)
    deliver(Event.from_json(event.to_json()))  # what the outbox re-hydrates
    assert _used(ws) == 4096
    assert StorageDeltaReceipt.objects.filter(event_id=event.event_id).count() == 1


def test_an_unknown_workspace_counts_nothing_and_keeps_no_receipt(ws):
    other = uuid.uuid4()
    assert apply_storage_delta(other, 10, event_id="e-1") is False
    assert not StorageDeltaReceipt.objects.filter(event_id="e-1").exists()
    assert _used(ws) == 0


@pytest.mark.parametrize(
    "payload",
    [
        {"workspace_id": "not-a-uuid", "delta_bytes": 5},
        {"workspace_id": None, "delta_bytes": 5},
        {"delta_bytes": 5},
        {"workspace_id": "11111111-1111-1111-1111-111111111111", "delta_bytes": "5"},
        {"workspace_id": "11111111-1111-1111-1111-111111111111", "delta_bytes": True},
    ],
)
def test_an_unusable_payload_is_dropped_not_retried(ws, payload):
    deliver(Event(event_type=STORAGE_CHANGED, service="docs", payload=payload))
    assert _used(ws) == 0
    assert not StorageDeltaReceipt.objects.exists()


def test_reconcile_sets_the_owners_figure_and_dry_run_reports_the_same(ws, docs_usage):
    deliver(_event(ws.pk, 100))
    docs_usage.figures[str(ws.pk)] = 7000

    out = StringIO()
    call_command("reconcile_storage_usage", "--dry-run", stdout=out)
    assert f"{ws.pk}: would set 100 -> 7000 (drift +6900)" in out.getvalue()
    assert _used(ws) == 100

    out = StringIO()
    call_command("reconcile_storage_usage", stdout=out)
    assert f"{ws.pk}: set 100 -> 7000 (drift +6900)" in out.getvalue()
    assert "checked 1; drifted 1; not measured 0" in out.getvalue()
    assert _used(ws) == 7000
    # The owner is asked as a service, not on behalf of a user.
    assert docs_usage.seen[-1]["caller_service"]
    assert "actor_id" not in docs_usage.seen[-1]

    # Deltas keep applying on top of the reconciled figure.
    deliver(_event(ws.pk, 10))
    assert _used(ws) == 7010


def test_reconcile_reports_a_workspace_it_could_not_measure(ws, user, docs_usage):
    measured = create_workspace(user=user, name="Measured")
    docs_usage.figures[str(measured.pk)] = 0
    out = StringIO()
    call_command("reconcile_storage_usage", stdout=out)
    assert f"{ws.pk}: not measured (LookupError" in out.getvalue() or (
        f"{ws.pk}: not measured" in out.getvalue()
    )
    assert "not measured 1" in out.getvalue()


def test_reconcile_with_no_owner_reachable_fails_loudly(ws):
    from django.core.management.base import CommandError

    with pytest.raises(CommandError):
        call_command("reconcile_storage_usage", stdout=StringIO())


def test_reconcile_one_function_for_both_modes(ws, docs_usage):
    docs_usage.figures[str(ws.pk)] = 42
    dry = reconcile_workspace(ws, apply=False)
    applied = reconcile_workspace(Workspace.objects.get(pk=ws.pk), apply=True)
    assert (dry.recorded, dry.measured, dry.drift) == (applied.recorded, applied.measured, applied.drift) == (0, 42, 42)


def test_old_receipts_are_pruned(ws, settings):
    settings.STAPEL_WORKSPACES = {**getattr(settings, "STAPEL_WORKSPACES", {}), "STORAGE_DELTA_RECEIPT_DAYS": 30}
    deliver(_event(ws.pk, 1, event_id="old"))
    deliver(_event(ws.pk, 1, event_id="new"))
    StorageDeltaReceipt.objects.filter(event_id="old").update(
        applied_at=timezone.now() - timedelta(days=31)
    )
    assert prune_receipts() == 1
    assert list(StorageDeltaReceipt.objects.values_list("event_id", flat=True)) == ["new"]
