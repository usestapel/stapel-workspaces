"""Set ``Workspace.storage_used_bytes`` to the storage owner's own figure.

The counter is fed by ``document.storage_changed`` deltas; this is the repair
for what deltas cannot see (bytes stored before the consumer existed, a
parked event). Asks ``STORAGE_USAGE_FUNCTION`` (default ``docs.usage``) per
workspace, so the storage owner must accept this service as a caller (for
stapel-docs: list it in ``STAPEL_DOCS['INTERNAL_TRUSTED_SERVICES']``). Run
once on adoption, then on a schedule; ``--dry-run`` reports the same rows an
apply writes. Deltas committed while it runs may be counted twice or not at
all for that workspace — run it when writes are quiet, or run it again.
"""
from django.core.management.base import BaseCommand, CommandError

from stapel_workspaces.models import Workspace
from stapel_workspaces.storage_usage import prune_receipts, reconcile_workspace


class Command(BaseCommand):
    help = "Reconcile Workspace.storage_used_bytes against the storage owner's figure"

    def add_arguments(self, parser):
        parser.add_argument(
            "--workspace", action="append", default=[],
            help="Workspace id to reconcile (repeatable); default: every live workspace",
        )
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        apply = not options["dry_run"]
        qs = Workspace.objects.filter(deleted_at__isnull=True).order_by("created_at")
        if options["workspace"]:
            qs = qs.filter(pk__in=options["workspace"])
        checked = drifted = failed = 0
        for workspace in qs.iterator():
            row = reconcile_workspace(workspace, apply=apply)
            checked += 1
            if row.error:
                failed += 1
                self.stdout.write(f"{row.workspace_id}: not measured ({row.error})")
            elif row.drift:
                drifted += 1
                verb = "set" if apply else "would set"
                self.stdout.write(
                    f"{row.workspace_id}: {verb} {row.recorded} -> {row.measured} "
                    f"(drift {row.drift:+d})"
                )
        pruned = prune_receipts() if apply else 0
        self.stdout.write(
            f"checked {checked}; drifted {drifted}; not measured {failed}; "
            f"receipts pruned {pruned}" + ("" if apply else " (dry run)")
        )
        if checked == 0:
            self.stdout.write("checked 0 workspaces")
        if failed and failed == checked:
            raise CommandError("no workspace could be measured")
