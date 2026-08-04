# Name hint fields (meettoday audit, 2026-08-04): the invite modal's "Имя"
# field had nowhere to go — `MemberInviteRequest` only carried
# `{emails, role}` — and the member list had nothing to show but email.
#
# `display_name_hint` is NOT the canonical name (that lives in
# stapel-profiles, per this module's own "never invent a second store of a
# field a sibling module already owns" convention) — it is a hint typed at
# invite/provision time, copied onto the member exactly once at creation and
# never touched again, shown only until stapel-profiles has a real name for
# the user. Expand-only (two new nullable-by-default columns, no data
# change) — safe both ways for the expand/contract migration gate.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('workspaces', '0004_member_provision_and_suspension'),
    ]

    operations = [
        migrations.AddField(
            model_name='workspaceinvitation',
            name='display_name_hint',
            field=models.CharField(blank=True, default='', max_length=35),
        ),
        migrations.AddField(
            model_name='workspacemember',
            name='display_name_hint',
            field=models.CharField(blank=True, default='', max_length=35),
        ),
    ]
