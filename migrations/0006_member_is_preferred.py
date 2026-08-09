# The person's explicit choice of home workspace (#239 follow-through).
#
# `STAPEL_WORKSPACES["DEFAULT_WORKSPACE_ID"]` already documents itself as a
# default that yields to "their explicit choice" — and that choice had
# nowhere to be recorded, so clients guessed. `is_preferred` is where it is
# written down.
#
# A flag on the membership rather than a user-level column on purpose: the
# preference then dies with the membership row, so removing a member cannot
# leave a pointer at a workspace they can no longer open, and no cleanup job
# is needed to keep that true.
#
# Expand-only: one new column with a default, plus a partial unique index
# ("at most one preferred membership per user"). Nothing existing sets the
# flag, so the index is satisfied by every row on the day it lands — safe
# both ways for the expand/contract migration gate.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('workspaces', '0005_member_invitation_display_name_hint'),
    ]

    operations = [
        migrations.AddField(
            model_name='workspacemember',
            name='is_preferred',
            field=models.BooleanField(default=False),
        ),
        migrations.AddConstraint(
            model_name='workspacemember',
            constraint=models.UniqueConstraint(
                condition=models.Q(('is_preferred', True)),
                fields=('user',),
                name='workspaces_member_one_preferred_per_user',
            ),
        ),
    ]
