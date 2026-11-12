"""``Workspace.storage_used_bytes``: one writer, fed by facts, checked by a reconcile.

The bytes belong to the module that stores them (stapel-docs today). It
announces ``document.storage_changed`` — ``{workspace_id, delta_bytes}``, a
signed delta emitted in the transaction that stored or freed them — and this
module keeps the counter. Nothing here imports the owner; the subscription and
the reconcile Function are names on the comm bus.

Why a delta and not an absolute total: a total read inside the owner's
transaction misses a concurrent transaction's bytes, so two saves in one
workspace would each publish a wrong total. A delta is exact; what it needs is
exactly-once application, which :class:`~.models.StorageDeltaReceipt` gives
(the envelope's ``event_id`` is unique, the receipt and the ``F()`` update
commit together, a redelivery is a no-op).

What deltas cannot fix — bytes stored before this consumer existed, a delta
lost to a parked event, a workspace restored from backup — is what
:func:`reconcile_workspace` is for: it asks the owner's
``STORAGE_USAGE_FUNCTION`` for the absolute figure and sets the counter to
it. Run it once when adopting the consumer, then on a schedule.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone

logger = logging.getLogger(__name__)

#: The fact this module counts.
STORAGE_CHANGED = "document.storage_changed"


def apply_storage_delta(workspace_id, delta_bytes: int, *, event_id: str) -> bool:
    """Move the counter by *delta_bytes* once per *event_id*.

    Returns ``True`` when the counter moved, ``False`` for a redelivery or a
    workspace this module does not hold (nothing to count; the receipt is
    not kept, so a later reconcile is the only repair and it is exact).
    """
    from .models import StorageDeltaReceipt, Workspace

    if not event_id:
        raise ValueError("a storage delta without an event_id cannot be deduplicated")
    with transaction.atomic():
        try:
            with transaction.atomic():
                StorageDeltaReceipt.objects.create(
                    event_id=str(event_id),
                    workspace_id=workspace_id,
                    delta_bytes=int(delta_bytes),
                )
        except IntegrityError:
            return False
        moved = Workspace.objects.filter(pk=workspace_id).update(
            storage_used_bytes=F("storage_used_bytes") + int(delta_bytes)
        )
        if not moved:
            transaction.set_rollback(True)
            return False
    return True


@dataclass
class ReconcileRow:
    workspace_id: str
    recorded: int
    measured: int | None
    error: str = ""

    @property
    def drift(self) -> int | None:
        return None if self.measured is None else self.measured - self.recorded


def measure(workspace_id) -> int:
    """The owner's absolute figure for one workspace (raises when it has none)."""
    from stapel_core.comm import call
    from stapel_core.comm.config import service_name

    from .conf import workspaces_settings

    name = workspaces_settings.STORAGE_USAGE_FUNCTION
    if not name:
        raise LookupError("STAPEL_WORKSPACES['STORAGE_USAGE_FUNCTION'] is empty")
    answer = call(
        name,
        {"workspace_id": str(workspace_id), "caller_service": service_name()},
        timeout=10.0,
    )
    total = (answer or {}).get("bytes_total")
    if not isinstance(total, int) or isinstance(total, bool):
        raise ValueError(f"{name} answered no integer bytes_total: {answer!r}")
    return total


def reconcile_workspace(workspace, *, apply: bool) -> ReconcileRow:
    """Compare (and with *apply*, set) one workspace's counter to the owner's figure.

    Dry-run and apply go through this one function, so a dry-run reports
    exactly what an apply would write.
    """
    from .models import Workspace

    row = ReconcileRow(
        workspace_id=str(workspace.pk), recorded=workspace.storage_used_bytes, measured=None
    )
    try:
        row.measured = measure(workspace.pk)
    except Exception as exc:  # noqa: BLE001 — reported per workspace, never fatal
        row.error = f"{type(exc).__name__}: {exc}"
        return row
    if apply and row.drift:
        Workspace.objects.filter(pk=workspace.pk).update(storage_used_bytes=row.measured)
    return row


def prune_receipts(*, now=None) -> int:
    """Forget receipts older than ``STORAGE_DELTA_RECEIPT_DAYS``; returns how many."""
    from .conf import workspaces_settings
    from .models import StorageDeltaReceipt

    days = int(workspaces_settings.STORAGE_DELTA_RECEIPT_DAYS)
    cutoff = (now or timezone.now()) - timedelta(days=days)
    deleted, _ = StorageDeltaReceipt.objects.filter(applied_at__lt=cutoff).delete()
    return deleted


def parse_workspace_id(value):
    """A UUID, or ``None`` for a payload that cannot name a workspace."""
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None
