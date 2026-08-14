"""Durable retry of the require_mfa sweep (WORK-01).

A workspace whose enforcement record is not ``enforced`` has members whose
second factor nobody has confirmed — because auth was unreachable when the
policy was switched on, or because they joined afterwards. This command is
the scheduled half of the answer (the lazy half is the admission gate,
which verifies one member at a time as they arrive); it is idempotent, so
running it every few minutes costs one auth call per unverified member and
nothing else.
"""
from django.core.management.base import BaseCommand

from stapel_workspaces.models import MFAEnforcementState
from stapel_workspaces.services import retry_mfa_enforcement


class Command(BaseCommand):
    help = "Re-run the require_mfa sweep for every workspace not yet enforced"

    def add_arguments(self, parser):
        parser.add_argument(
            "--limit",
            type=int,
            default=100,
            help="Maximum workspaces to sweep in one run (default 100).",
        )

    def handle(self, *args, **options):
        records = retry_mfa_enforcement(limit=options["limit"])
        for record in records:
            self.stdout.write(
                f"{record.workspace_id}: {record.state} "
                f"(checked {record.checked_members}, "
                f"noncompliant {record.noncompliant_members}"
                + (f", error {record.last_error}" if record.last_error else "")
                + ")"
            )
        unfinished = sum(
            1 for r in records if r.state != MFAEnforcementState.ENFORCED
        )
        self.stdout.write(
            f"swept {len(records)} workspace(s); {unfinished} still incomplete"
        )
