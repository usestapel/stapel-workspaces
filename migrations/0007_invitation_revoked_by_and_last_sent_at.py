# The two facts an invitation could not state about itself (0.23).
#
# `revoked_by` — who withdrew it. `revoked_at` said when, and the actor
# existed only inside the emitted `workspace.invitation_revoked` event: a
# message on a bus, not a record this service can be asked a question about
# afterwards. Any UI could therefore show the time of a permissioned action
# and never the person behind it. Same FK/SET_NULL provenance shape as
# `invited_by` on the opposite transition.
#
# `last_sent_at` — when a letter for this invitation last went out. The
# resend path had neither a cooldown nor any memory of the previous send,
# so an address could be mailed in a loop through this fleet's mail
# infrastructure. This column is the clock
# `STAPEL_WORKSPACES["INVITATION_RESEND_COOLDOWN_SECONDS"]` reads. NULL on
# every pre-existing row means "no letter recorded", which reads as "no
# cooldown owed" — an invitation created before this migration is
# resendable immediately, exactly as it was.
#
# Expand-only: two new nullable columns, no data change, no backfill —
# safe both ways for the expand/contract migration gate.

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('workspaces', '0006_member_is_preferred'),
    ]

    operations = [
        migrations.AddField(
            model_name='workspaceinvitation',
            name='revoked_by',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='revoked_workspace_invitations',
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name='workspaceinvitation',
            name='last_sent_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
