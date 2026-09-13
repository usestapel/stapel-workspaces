"""What ``workspace.personal.created`` has to tell its consumers.

The event used to carry ``{user_id, workspace_id}``. That is enough to find a
workspace and not enough to mirror an account, and a consumer needs the
second thing: it is in exactly the position this command was in before
0.30.3 — about to write a row with a foreign key to ``users``, with no local
row for that user, because its own writer of one (core's JWT seam) runs on
the account's first authenticated request to THAT service and the account has
not made one. iron-recordings, 2026-09-13: three of these events arrived, all
three died on ``ForeignKeyViolation … Key (user_id)=(13484e5c-…) is not
present in table "users"`` and all three parked in the DLQ.

A consumer can materialise the row from the event — but with the id alone it
has to take the model's defaults, so a guest lands as ``is_anonymous=False,
auth_type="email"`` and stays wrong until their first request there repairs
it. This command has the account in hand, so it says who it is.

The other half: ``_mirror_user`` now goes through core's event-facing seam
(``ensure_shadow_user``) rather than the JWT one. Behaviour here is
unchanged — this consumer only mirrors when there is no row — and the point
is that "materialise a user from an event" has ONE implementation in the
fleet instead of three. The tests below pin what that seam guarantees
through this path: an existing row is not touched, a tombstoned account is
not revived, and a guest is mirrored AS a guest.
"""
import uuid
from io import StringIO

import pytest
from django.contrib.auth import get_user_model
from django.test import override_settings

from stapel_workspaces.models import Workspace, WorkspaceType


def _command():
    from stapel_workspaces.management.commands.consume_auth_events import Command

    return Command(stdout=StringIO(), stderr=StringIO())


def _event(payload, event_type="user.registered"):
    from stapel_core.bus import Event

    return Event(event_type=event_type, service="auth", payload=payload)


def _registered(user_id, **extra):
    payload = {
        "user_id": str(user_id),
        "auth_type": "anonymous",
        "email": None,
        "avatar_url": None,
        "language": None,
        "display_name": None,
        "is_anonymous": True,
    }
    payload.update(extra)
    return payload


@pytest.fixture
def consumer_mode():
    with override_settings(JWT_CREATE_USERS_FROM_TOKEN=True):
        yield


@pytest.mark.django_db
@pytest.mark.usefixtures("consumer_mode")
class TestThePayloadCarriesTheIdentity:
    def _publish_for(self, payload):
        from stapel_core import bus

        seen = []
        real = bus.publish

        def _capture(name, event, *a, **kw):
            if getattr(event, "event_type", None) == "workspace.personal.created":
                seen.append(dict(event.payload))
            return real(name, event, *a, **kw)

        mp = pytest.MonkeyPatch()
        mp.setattr(bus, "publish", _capture)
        try:
            _command().handle_event(_event(payload))
        finally:
            mp.undo()
        return seen

    def test_a_guest_is_announced_as_a_guest(self):
        uid = uuid.uuid4()
        seen = self._publish_for(_registered(uid))

        assert len(seen) == 1
        assert seen[0]["user_id"] == str(uid)
        assert seen[0]["is_anonymous"] is True
        assert seen[0]["auth_type"] == "anonymous"
        assert seen[0]["email"] is None

    def test_a_named_account_carries_its_email(self):
        uid = uuid.uuid4()
        seen = self._publish_for(_registered(
            uid, auth_type="email", is_anonymous=False, email="new@example.com",
        ))
        assert seen[0]["is_anonymous"] is False
        assert seen[0]["auth_type"] == "email"
        assert seen[0]["email"] == "new@example.com"

    def test_the_workspace_id_is_still_there(self):
        uid = uuid.uuid4()
        seen = self._publish_for(_registered(uid))
        ws = Workspace.objects.get(owner_id=uid, type=WorkspaceType.PERSONAL)
        assert seen[0]["workspace_id"] == str(ws.id)

    def test_no_privileges_are_announced(self):
        """A consumer must not be able to read a staff flag off this event."""
        uid = uuid.uuid4()
        seen = self._publish_for(_registered(uid, is_staff=True, is_superuser=True))
        assert "is_staff" not in seen[0]
        assert "is_superuser" not in seen[0]
        assert "staff_roles" not in seen[0]

    def test_the_payload_matches_its_own_schema(self):
        """``additionalProperties: false`` — a widened emit needs the schema
        widened with it, or the contract says one thing and the bus another."""
        import json
        import pathlib

        import stapel_workspaces

        schema = json.loads(
            (
                pathlib.Path(stapel_workspaces.__file__).parent
                / "schemas" / "emits" / "workspace.personal.created.json"
            ).read_text()
        )
        seen = self._publish_for(_registered(uuid.uuid4()))
        allowed = set(schema["properties"])
        assert set(seen[0]) <= allowed, set(seen[0]) - allowed
        assert set(schema["required"]) <= set(seen[0])


@pytest.mark.django_db
@pytest.mark.usefixtures("consumer_mode")
class TestTheMirrorGoesThroughTheEventSeam:
    def test_an_existing_row_is_not_touched(self):
        """A user this service already knows is read, never re-synced.

        Worth pinning rather than assuming: ``get_or_create_user_from_jwt``
        REPLACES ``is_staff``/``is_superuser`` from the claims in consumer
        mode, and an event carries none — so a seam that fed the payload
        through it on every call would read that silence as ``False`` and
        demote a staff row from a handler whose only business was a foreign
        key. This consumer never had that exposure (it mirrors only when the
        row is missing); ``ensure_shadow_user``, which handlers DO call
        unconditionally, closes it by returning the existing row untouched.
        """
        User = get_user_model()
        uid = uuid.uuid4()
        User.objects.create_user(
            pk=uid, username="ops_admin", email="ops@example.com",
            is_staff=True, is_superuser=True,
        )

        _command().handle_event(_event(_registered(
            uid, auth_type="email", is_anonymous=False, email="ops@example.com",
        )))

        row = User.objects.get(pk=uid)
        assert row.is_staff is True
        assert row.is_superuser is True

    def test_a_deleted_account_is_not_revived_by_the_event(self, monkeypatch):
        """The gates in front of the seam still hold through this path."""
        uid = uuid.uuid4()
        monkeypatch.setattr(
            "stapel_core.django.jwt.utils._tombstoned", lambda u: str(u) == str(uid)
        )
        cmd = _command()
        cmd.handle_event(_event(_registered(uid)))

        assert not get_user_model().objects.filter(pk=uid).exists()
        assert Workspace.objects.count() == 0
        assert "not found, skipping" in cmd.stderr.getvalue()

    def test_a_guest_row_is_mirrored_as_a_guest(self):
        uid = uuid.uuid4()
        _command().handle_event(_event(_registered(uid)))

        row = get_user_model().objects.get(pk=uid)
        assert row.is_anonymous is True
        assert row.auth_type == "anonymous"
        assert row.email is None
