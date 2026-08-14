from django.conf import settings
from django.db import migrations, models
from django.utils import timezone


def revoke_duplicate_live_invitations(apps, schema_editor):
    """Collapse pre-existing duplicates so the constraint can be created.

    Rows written before the one-live-invitation rule existed: the newest
    unresolved invitation for an address keeps the seat and the working
    token, its older twins are revoked (the workspace's own terminal "no",
    the honest label for a link the org is withdrawing). No audit line is
    written — nobody performed this, a version did, and the journal records
    actors.
    """
    Invitation = apps.get_model("workspaces", "WorkspaceInvitation")
    live = Invitation.objects.filter(
        accepted_at__isnull=True, declined_at__isnull=True, revoked_at__isnull=True
    ).order_by("workspace_id", "email", "-created_at")
    now = timezone.now()
    seen = set()
    duplicates = []
    for invitation in live.iterator():
        key = (invitation.workspace_id, invitation.email)
        if key in seen:
            duplicates.append(invitation.pk)
        else:
            seen.add(key)
    if duplicates:
        Invitation.objects.filter(pk__in=duplicates).update(revoked_at=now)


class Migration(migrations.Migration):

    dependencies = [
        ('workspaces', '0009_audit_into_the_eventstore'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RunPython(
            revoke_duplicate_live_invitations,
            migrations.RunPython.noop,
            elidable=False,
        ),
        migrations.AddConstraint(
            model_name='workspaceinvitation',
            constraint=models.UniqueConstraint(condition=models.Q(('accepted_at__isnull', True), ('declined_at__isnull', True), ('revoked_at__isnull', True)), fields=('workspace', 'email'), name='workspaces_invitation_one_live_per_email'),
        ),
    ]
