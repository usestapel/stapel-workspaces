"""Settle the refunds a failed provisioning could not make (WORK-03).

Provisioning charges billing before it can know whether auth will mint the
account. When a later step fails, the saga tries the refund immediately and,
if billing cannot take it, leaves the debt on the operation row. This
command is the queue's consumer: idempotent (the refund carries a
per-operation key), safe to schedule, and loud about what is still owed.
"""
from django.core.management.base import BaseCommand

from stapel_workspaces.models import ProvisionState, WorkspaceProvisionOperation
from stapel_workspaces.services import reconcile_provision_operations


class Command(BaseCommand):
    help = "Retry refunds owed for provisioning operations that did not complete"

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=100)

    def handle(self, *args, **options):
        settled = reconcile_provision_operations(limit=options["limit"])
        for operation in settled:
            self.stdout.write(f"{operation.operation_id}: refunded")
        still_owed = WorkspaceProvisionOperation.objects.filter(
            state=ProvisionState.COMPENSATING
        ).count()
        self.stdout.write(f"settled {len(settled)}; {still_owed} still owed")
        orphans = WorkspaceProvisionOperation.objects.filter(
            state=ProvisionState.ACCOUNT_CREATED
        ).count()
        if orphans:
            # Deleting an auth account is not this module's to do; naming
            # it is. An operation that minted an account and never wrote a
            # membership is exactly what a human has to clean up.
            self.stdout.write(
                f"{orphans} operation(s) left an auth account with no membership"
            )
