"""Out-of-process brownfield check.

Configures Django with AUTH_USER_MODEL pointed at a *custom* AbstractStapelUser
subclass (brownfield_users.CustomUser), then creates a Workspace and a
WorkspaceMember with a custom-user instance. If the workspaces FKs were pinned
to the concrete stapel_core users.User (the pre-0.3.3 bug), assigning a
CustomUser instance would raise:

    ValueError: Cannot assign "<CustomUser>": "WorkspaceMember.user" must be a
    "User" instance.

Runs in its own process because AUTH_USER_MODEL cannot be swapped after Django's
app registry is populated (the default test suite already pins it to users.User).
Prints "BROWNFIELD_OK" on success.
"""

import django
from django.conf import settings

settings.configure(
    SECRET_KEY="test-brownfield",
    INSTALLED_APPS=[
        "django.contrib.contenttypes",
        "django.contrib.auth",
        "stapel_workspaces.tests.brownfield_users",
        "stapel_workspaces",
    ],
    # The host's own user model — NOT stapel_core's concrete users.User.
    AUTH_USER_MODEL="brownfield_users.CustomUser",
    DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}},
    DEFAULT_AUTO_FIELD="django.db.models.BigAutoField",
    USE_TZ=True,
    # Build tables straight from models — same approach as conftest.
    MIGRATION_MODULES={"workspaces": None, "brownfield_users": None},
)
django.setup()

from django.contrib.auth import get_user_model  # noqa: E402
from django.core.management import call_command  # noqa: E402


def main() -> None:
    call_command("migrate", run_syncdb=True, verbosity=0)

    from stapel_workspaces.models import Role, Workspace, WorkspaceMember

    User = get_user_model()
    assert User.__name__ == "CustomUser", User

    # The FKs must resolve to the host's swapped user model, not the concrete one.
    assert Workspace._meta.get_field("owner").related_model is User
    assert WorkspaceMember._meta.get_field("user").related_model is User
    assert WorkspaceMember._meta.get_field("invited_by").related_model is User

    owner = User.objects.create_user(username="owner", password="x")
    ws = Workspace.objects.create(name="Acme", slug="acme", owner=owner)
    # This assignment is exactly what raised ValueError pre-fix.
    member = WorkspaceMember.objects.create(
        workspace=ws, user=owner, role=Role.OWNER, invited_by=owner
    )
    assert member.user_id == owner.pk
    print("BROWNFIELD_OK")


if __name__ == "__main__":
    main()
