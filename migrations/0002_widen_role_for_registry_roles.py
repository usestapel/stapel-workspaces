# Widen the role columns for registry roles (org-program spec §A1):
# custom product roles from STAPEL_WORKSPACES["ROLES"] may be up to 32
# chars. Expand-only (column widening, no data change) — safe both ways
# for the expand/contract migration gate.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('workspaces', '0001_initial'),
    ]

    operations = [
        migrations.AlterField(
            model_name='workspacemember',
            name='role',
            field=models.CharField(choices=[('owner', 'Owner'), ('admin', 'Admin'), ('member', 'Member'), ('viewer', 'Viewer')], default='member', max_length=32),
        ),
        migrations.AlterField(
            model_name='workspaceinvitation',
            name='role',
            field=models.CharField(choices=[('owner', 'Owner'), ('admin', 'Admin'), ('member', 'Member'), ('viewer', 'Viewer')], default='member', max_length=32),
        ),
    ]
