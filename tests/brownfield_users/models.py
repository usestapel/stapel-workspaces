"""A brownfield host's custom user model.

Simulates the real adoption case: a downstream project that subclasses
``AbstractStapelUser`` and points ``AUTH_USER_MODEL`` at it. Used only by the
out-of-process brownfield test (never part of the default test app), which is
why it lives in its own app package rather than in ``conftest``.
"""

from stapel_core.django.users.models import AbstractStapelUser


class CustomUser(AbstractStapelUser):
    class Meta:
        app_label = "brownfield_users"
        db_table = "brownfield_users_customuser"
