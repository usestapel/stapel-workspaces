from stapel_core.gdpr import GDPRProvider


class WorkspacesGDPRProvider(GDPRProvider):
    section = 'workspaces'

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
        from .models import Workspace, WorkspaceInvitation, WorkspaceMember

        # Remove memberships
        WorkspaceMember.objects.filter(user_id=user_id).delete()

        # Revoke pending invitations sent by this user
        WorkspaceInvitation.objects.filter(
            invited_by_id=user_id, accepted_at__isnull=True,
        ).delete()

        # Owned workspaces: soft-delete (mark deleted_at).
        # Hard deletion of workspace content is out of scope here —
        # the platform should handle workspace transfer/deletion separately.
        from django.utils import timezone
        Workspace.objects.filter(owner_id=user_id, deleted_at__isnull=True).update(
            deleted_at=timezone.now(),
        )

    def anonymize(self, user_id: int) -> None:
        from .models import WorkspaceInvitation, WorkspaceMember

        # Keep accepted invitation records but remove the invited_by link
        WorkspaceInvitation.objects.filter(
            invited_by_id=user_id, accepted_at__isnull=False,
        ).update(invited_by=None)

        # Membership records that need to stay (e.g. for workspace history)
        # are already removed in delete(); nothing to anonymise here.


def _serialize_dates(rows: list[dict]) -> list[dict]:
    return [
        {k: v.isoformat() if hasattr(v, 'isoformat') else v for k, v in row.items()}
        for row in rows
    ]
