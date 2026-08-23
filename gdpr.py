from stapel_core.gdpr import GDPRProvider

from .erasure import GDPR_OWNER


class WorkspacesGDPRProvider(GDPRProvider):
    #: Same name the comm receipts and probe answers carry — one owner, one
    #: declaration in ``STAPEL_GDPR["DATA_OWNERS"]``, whichever of the two
    #: participation modes a deployment uses.
    section = GDPR_OWNER

    def export(self, user_id: int) -> dict:
        from .models import Workspace, WorkspaceInvitation, WorkspaceMember

        memberships = list(WorkspaceMember.objects.filter(user_id=user_id).select_related('workspace').values(
            'workspace__name', 'workspace__slug', 'workspace__type',
            'role', 'invited_at', 'accepted_at', 'last_accessed_at',
        ))

        owned = list(Workspace.objects.filter(owner_id=user_id).values(
            'name', 'slug', 'type', 'storage_used_bytes', 'created_at',
        ))

        sent_invitations = list(WorkspaceInvitation.objects.filter(invited_by_id=user_id).values(
            'workspace__name', 'role', 'created_at', 'accepted_at',
        ))

        return {
            'memberships':      _serialize_dates(memberships),
            'owned_workspaces': _serialize_dates(owned),
            'invitations_sent': _serialize_dates(sent_invitations),
        }

    def delete(self, user_id: int) -> None:
        """Erase the account slice — one implementation, three callers.

        The in-process provider, the deprecated ``user.deleted`` subscriber
        and the ``gdpr.erasure.requested`` subscriber all reach
        :mod:`stapel_workspaces.erasure`, so a deployment cannot get a
        different erasure depending on which participation mode it happens
        to use. The comm path calls ``erase_account`` (both halves); this
        one calls the destroying half only, because the protocol runs
        :meth:`anonymize` separately.
        """
        from .erasure import delete_account_rows

        delete_account_rows(user_id)

    def anonymize(self, user_id: int) -> None:
        # Keep accepted invitation records but remove the invited_by link.
        # NB: an INVITATION predicate, not a membership one — same column
        # name, different model, different question.
        from .erasure import anonymize_account_rows

        anonymize_account_rows(user_id)


def _serialize_dates(rows: list[dict]) -> list[dict]:
    return [
        {k: v.isoformat() if hasattr(v, 'isoformat') else v for k, v in row.items()}
        for row in rows
    ]
