"""The membership journal, stored in the core event store.

Until 0.24.x this module kept its history in a bespoke append-only table
(``WorkspaceAuditEvent``) — built the very defect this fleet keeps
re-finding: core already had the general audit facility (an append-only
stream primitive with retention, rollups, cursor reads and a pluggable
backend — ``stapel_core.eventstore``, the store the privilege gateway's
audit already writes to), and a consumer reinvented it one floor up. So the
journal now IS a stream: ``STAPEL_WORKSPACES["AUDIT_STREAM"]`` (default
``workspace.audit``), written through the ``AUDIT_SINK`` seam and read back
through the store's anchor adapter, which speaks the exact wire contract
``GET <workspace_id>/audit`` shipped with in 0.24 — the endpoint's shape
did not move when the storage did.

What stays here is everything the store must not know: the closed action
vocabulary (``models.AuditAction``), who may read a workspace's history
(``views.WorkspaceAuditView``), and the meaning of the payload fields.

One line's payload::

    {"id": "<uuid>", "workspace_id": "<uuid>", "action": "member_removed",
     "actor_id": "<uuid, when someone did it>",
     "subject_id": "<uuid, when it happened to an account>",
     "subject_email": "", "role": "", "metadata": {...}}

``actor_id``/``subject_id`` are the canonical audit identity keys —
``manage.py audit_trail`` (stapel-core) matches them when an operator asks
for one person's history across every module's stream.
"""
from __future__ import annotations

import logging
import uuid as uuid_module

from .conf import workspaces_settings

logger = logging.getLogger(__name__)


def eventstore_sink(stream: str, payload: dict, *, project=None, container=None) -> None:
    """Default sink: append to the core event store, then flush.

    Same callable contract as the gateway's ``STAPEL_GATEWAY["AUDIT_SINK"]``,
    so one custom sink implementation can serve both seams.

    The flush is deliberate and is the difference from the gateway default:
    membership transitions happen at human cadence and the admin UI re-reads
    the history right after performing one. The store's write buffer is
    per-process, so in a multi-worker deployment an unflushed line would be
    invisible to the worker serving the very next GET — the endpoint would
    lie for up to the buffer interval about the action it just recorded.
    Flushing one row per membership change costs nothing at this volume.
    """
    from stapel_core import eventstore

    eventstore.append(stream, payload, project=project, container=container)
    eventstore.flush()


def record_audit(
    *,
    workspace,
    action: str,
    actor=None,
    subject=None,
    subject_email: str = "",
    role: str = "",
    **metadata,
) -> dict | None:
    """Append one line to a workspace's membership history.

    THE ONE WRITE PATH, and it is called from the SERVICE that owns each
    transition rather than from the views — the same rule the emits already
    follow, for the same reason: a second door into a transition would come
    with a second chance to forget the record. ``tests/test_audit.py`` pins
    that every emitted membership event has a matching audit action, so a
    future transition cannot ship emitting-but-not-recording.

    *actor* and *subject* accept a user object or a bare id — call sites hold
    one or the other and normalising here beats `getattr(x, "pk", x)` at ten
    of them.

    Never raises into the caller: an audit line is a record OF the change, not
    a precondition FOR it, and failing a removal because history could not be
    written would be the tail wagging the dog. A failure is logged loudly —
    silence here would make the history quietly incomplete, which is worse
    than a gap somebody can see. (The gateway makes the opposite choice —
    fail-closed — because there the line gates a PRIVILEGED INVOCATION;
    here it trails a domain fact that has already happened.)

    Returns the payload written, or ``None`` when the sink failed.
    """

    def _id(value):
        if value is None:
            return None
        return getattr(value, "pk", value)

    payload = {
        # Minted here, not by the store: the line's identity must survive a
        # backend swap, and 0.24.x rows already carried UUID ids the API
        # exposes — new lines keep the same shape.
        "id": str(uuid_module.uuid4()),
        "workspace_id": str(getattr(workspace, "pk", workspace)),
        "action": action,
        "subject_email": (subject_email or "").lower().strip(),
        "role": role or "",
        "metadata": {k: v for k, v in metadata.items() if v is not None},
    }
    actor_id = _id(actor)
    subject_id = _id(subject)
    # Present-when-known, never null: audit_trail and the read filters match
    # on the key, and a key that is sometimes null matches nothing cleanly.
    if actor_id is not None:
        payload["actor_id"] = str(actor_id)
    if subject_id is not None:
        payload["subject_id"] = str(subject_id)

    try:
        sink = workspaces_settings.AUDIT_SINK
        sink(str(workspaces_settings.AUDIT_STREAM), payload, project=None, container=None)
        return payload
    except Exception:  # noqa: BLE001 - see the docstring
        logger.exception(
            "workspaces: could not record audit action %s for workspace %s",
            action,
            getattr(workspace, "pk", workspace),
        )
        return None


def history_page(
    workspace_id,
    *,
    action: str = "",
    subject_id=None,
    anchor=None,
    direction: str = "next",
    limit: int = 100,
):
    """One anchor page of a workspace's history, newest first.

    A thin scope over the store's anchor adapter: the workspace bound is
    applied HERE, unconditionally, so no caller can reach another
    workspace's lines by forgetting a filter. Raises ``ValueError`` on a
    malformed anchor (the adapter's contract) — the view maps it.
    """
    from stapel_core.eventstore.anchor import anchor_page

    filters: dict[str, object] = {"workspace_id": str(workspace_id)}
    if action:
        filters["action"] = action
    if subject_id is not None:
        filters["subject_id"] = str(subject_id)
    return anchor_page(
        str(workspaces_settings.AUDIT_STREAM),
        filters=filters,
        anchor=anchor,
        direction=direction,
        limit=limit,
    )


def replay_legacy_rows(rows) -> int:
    """Move rows of the retired ``workspaces_audit_event`` table into the
    stream — the data path of the deletion-driven migration (0009).

    *rows* are dicts of the old columns. Original timestamps become the
    event ``ts`` and original UUID primary keys stay the line's ``id``, so a
    page an admin bookmarked before the migration reads identically after
    it: same order, same anchors, same ids.

    Writes through the facade, not a table: a deployment that routed the
    audit stream to another backend gets its history there, not in a table
    it never reads.
    """
    from stapel_core import eventstore
    from stapel_core.eventstore import Event

    stream = str(workspaces_settings.AUDIT_STREAM)
    moved = 0
    batch: list[Event] = []
    for row in rows:
        payload = {
            "id": str(row["id"]),
            "workspace_id": str(row["workspace_id"]),
            "action": row["action"],
            "subject_email": row.get("subject_email") or "",
            "role": row.get("role") or "",
            "metadata": row.get("metadata") or {},
        }
        if row.get("actor_id"):
            payload["actor_id"] = str(row["actor_id"])
        if row.get("subject_id"):
            payload["subject_id"] = str(row["subject_id"])
        batch.append(Event(stream=stream, payload=payload, ts=row["created_at"]))
        if len(batch) >= 500:
            eventstore.append_batch(batch)
            moved += len(batch)
            batch = []
    if batch:
        eventstore.append_batch(batch)
        moved += len(batch)
    eventstore.flush()
    return moved


__all__ = [
    "eventstore_sink",
    "history_page",
    "record_audit",
    "replay_legacy_rows",
]
