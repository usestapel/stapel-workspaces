# Provision + suspension fields on WorkspaceMember (org-program spec §C1/§C3,
# Wave 3): `provisioned` marks org-created (synthetic) members joined via
# POST members/provision; `suspended_at`/`suspension_reason` implement the
# require_mfa policy as suspension-not-removal — access checks count only
# rows with suspended_at IS NULL. Expand-only (new nullable/defaulted
# columns, no data change) — safe both ways for the expand/contract gate.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('workspaces', '0003_invitation_declined_at'),
    ]

    operations = [
        migrations.AddField(
            model_name='workspacemember',
            name='provisioned',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='workspacemember',
            name='suspended_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='workspacemember',
            name='suspension_reason',
            field=models.CharField(blank=True, default='', max_length=32),
        ),
    ]
