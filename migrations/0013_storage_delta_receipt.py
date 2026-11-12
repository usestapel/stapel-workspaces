from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('workspaces', '0012_provision_saga_and_login_grant'),
    ]

    operations = [
        migrations.CreateModel(
            name='StorageDeltaReceipt',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('event_id', models.CharField(max_length=64, unique=True)),
                ('workspace_id', models.UUIDField()),
                ('delta_bytes', models.BigIntegerField()),
                ('applied_at', models.DateTimeField(auto_now_add=True)),
            ],
            options={
                'db_table': 'workspaces_storage_delta_receipt',
                'indexes': [models.Index(fields=['applied_at'], name='workspaces__applied_b03344_idx')],
            },
        ),
    ]
