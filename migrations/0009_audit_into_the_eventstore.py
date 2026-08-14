# The membership journal moves into the core event store (see audit.py for
# why the bespoke table was a defect, not a design). Deletion-driven: the
# same migration that retires the table carries its rows out — original
# timestamps become the event ts and original UUID primary keys stay the
# line's id, so every page served before the migration reads identically
# after it (same order, same anchors, same item ids).
from django.db import migrations


def replay_into_eventstore(apps, schema_editor):
    # Live code on purpose, not a frozen copy: replay_legacy_rows writes
    # through the eventstore facade, so a deployment that routed the audit
    # stream to another backend (STAPEL_EVENTSTORE["ROUTES"]) receives its
    # history THERE — a raw-model copy here would silently pin everyone to
    # the default table. The helper reads only stable column names, which
    # is the part of the historical model that matters.
    from stapel_workspaces.audit import replay_legacy_rows

    OldEvent = apps.get_model("workspaces", "WorkspaceAuditEvent")
    replay_legacy_rows(
        OldEvent.objects.order_by("created_at", "id")
        .values(
            "id",
            "workspace_id",
            "action",
            "actor_id",
            "subject_id",
            "subject_email",
            "role",
            "metadata",
            "created_at",
        )
        .iterator()
    )


class Migration(migrations.Migration):

    dependencies = [
        ("workspaces", "0008_audit_event"),
        # The default backend's table must exist before rows land in it. A
        # deployment on a non-default backend still needs the app installed
        # (the dependency is structural); its table just stays empty.
        ("stapel_eventstore", "0001_initial"),
    ]

    operations = [
        # Copy first, drop second — the reverse order would be a deletion
        # with no data path. Backwards is a no-op: replayed events stay in
        # the store (re-running forward from a recreated empty table then
        # moves nothing, so there is no duplication hazard either).
        migrations.RunPython(replay_into_eventstore, migrations.RunPython.noop),
        migrations.DeleteModel(name="WorkspaceAuditEvent"),
    ]
