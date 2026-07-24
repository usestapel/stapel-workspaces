# Invitation decline (org-program spec §B1-B2, Wave 2): a `declined_at`
# timestamp distinct from `revoked_at` — decline is the invitee's action,
# revoke is the workspace's, and both stay distinguishable in the derived
# `status`. Expand-only (new nullable column, no data change) — safe both
# ways for the expand/contract migration gate.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('workspaces', '0002_widen_role_for_registry_roles'),
    ]

    operations = [
        migrations.AddField(
            model_name='workspaceinvitation',
            name='declined_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
