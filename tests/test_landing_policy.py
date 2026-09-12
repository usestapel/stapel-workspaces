"""``resolve_landing_workspace`` — the landing-mandate policy axis (org-program #85).

Mandate-model vardict (2026-08-03): before this canon, the ONLY primitive
was ``ensure_personal_workspace``, unconditional by construction — every
product subscriber to ``user.registered`` that called it made "every fresh
signup becomes OWNER of a personal workspace" inescapable. This module is
the seam a host uses to choose otherwise (``STREET_LANDING_MODE``) instead
of forking the subscriber.

The default ("personal") is the pre-#85 behavior byte-for-byte: these tests
are also the back-compat gate — an existing deployment that never touches
``STAPEL_WORKSPACES["STREET_LANDING_MODE"]`` must see NO change.
"""
import pytest
from django.test import override_settings

from stapel_workspaces.models import Role, Workspace, WorkspaceMember, WorkspaceType
from stapel_workspaces.services import ensure_personal_workspace, resolve_landing_workspace


@pytest.mark.django_db
class TestDefaultIsBackCompat:
    """No STREET_LANDING_MODE override in settings → identical to the old code."""

    def test_default_street_origin_gets_a_personal_workspace(self, user):
        ws = resolve_landing_workspace(user, origin="street")
        assert ws is not None
        assert ws.type == WorkspaceType.PERSONAL
        assert ws.owner_id == user.pk
        assert WorkspaceMember.objects.get(workspace=ws, user=user).role == Role.OWNER

    def test_default_matches_ensure_personal_workspace_directly(self, user):
        """Same object ``ensure_personal_workspace`` alone would have returned —
        the canon must not be a DIFFERENT personal workspace or a second one."""
        resolved = resolve_landing_workspace(user, origin="street")
        direct = ensure_personal_workspace(user)
        assert resolved.id == direct.id
        assert Workspace.objects.filter(owner=user, type=WorkspaceType.PERSONAL).count() == 1

    def test_anon_origin_also_defaults_to_personal(self, user):
        """"anon" is just another un-invited origin — same policy as "street"."""
        ws = resolve_landing_workspace(user, origin="anon")
        assert ws is not None
        assert ws.type == WorkspaceType.PERSONAL

    def test_repeated_calls_are_idempotent(self, user):
        """A user landing twice (two registrations of the same account path,
        or a retried subscriber) must not accumulate personal workspaces."""
        first = resolve_landing_workspace(user, origin="street")
        second = resolve_landing_workspace(user, origin="street")
        assert first.id == second.id
        assert Workspace.objects.filter(owner=user).count() == 1


@pytest.mark.django_db
class TestInvitedOriginIsANoOp:
    """``origin="invited"`` never mints anything here — accept_invitation owns it."""

    def test_invited_origin_returns_none(self, user):
        assert resolve_landing_workspace(user, origin="invited") is None

    def test_invited_origin_creates_no_workspace(self, user):
        resolve_landing_workspace(user, origin="invited")
        assert not Workspace.objects.filter(owner=user).exists()

    @override_settings(STAPEL_WORKSPACES={"STREET_LANDING_MODE": "personal"})
    def test_invited_origin_ignores_the_street_mode_setting(self, user):
        """The axis governs un-invited origins only — an org's own
        invite-accept path must never spawn an extra personal workspace,
        no matter how STREET_LANDING_MODE is configured."""
        assert resolve_landing_workspace(user, origin="invited") is None
        assert not Workspace.objects.filter(owner=user).exists()


@pytest.mark.django_db
class TestNoneModeIsTheClosedOrgAxis:
    """The behavior change this canon exists to make possible."""

    @override_settings(STAPEL_WORKSPACES={"STREET_LANDING_MODE": "none"})
    def test_street_origin_gets_no_workspace(self, user):
        assert resolve_landing_workspace(user, origin="street") is None
        assert not Workspace.objects.filter(owner=user).exists()

    @override_settings(STAPEL_WORKSPACES={"STREET_LANDING_MODE": "none"})
    def test_the_resulting_user_is_a_guest(self, user):
        """The point of the axis: a "none"-landed account reads as a guest —
        the predicate permissions.is_guest exists for exactly this."""
        from stapel_workspaces.permissions import is_guest

        resolve_landing_workspace(user, origin="street")
        assert is_guest(user) is True

    @override_settings(STAPEL_WORKSPACES={"STREET_LANDING_MODE": "none"})
    def test_invited_origin_still_unaffected_by_none_mode(self, user):
        assert resolve_landing_workspace(user, origin="invited") is None
        assert not Workspace.objects.filter(owner=user).exists()


@pytest.mark.django_db
class TestUnknownModeFailsClosed:
    """An admin typo in the setting must not silently resurrect the old default."""

    @override_settings(STAPEL_WORKSPACES={"STREET_LANDING_MODE": "not-a-real-mode"})
    def test_unrecognized_mode_creates_no_workspace(self, user):
        assert resolve_landing_workspace(user, origin="street") is None
        assert not Workspace.objects.filter(owner=user).exists()


@pytest.mark.django_db
class TestClosedOrgModeDoesNotTouchTheRealInviteAcceptPath:
    """The axis governs un-invited landings; an accepted invite is a
    COMPLETELY separate mechanism (:func:`services.accept_invitation`) that
    never consults ``STREET_LANDING_MODE`` at all. Pinned end-to-end (not
    just via ``resolve_landing_workspace``'s ``origin="invited"`` no-op)
    because this is the real path a product's invite flow drives — a
    closed-organization deployment must not accidentally strand its own
    invited members without a mandate.
    """

    @override_settings(STAPEL_WORKSPACES={"STREET_LANDING_MODE": "none"})
    def test_invited_user_still_gets_a_membership(self, user, other_user):
        from stapel_workspaces import services

        ws = services.create_workspace(user=user, name="Acme")
        invitation = services.create_invitation(
            workspace=ws, email="x@example.com", role=Role.MEMBER, invited_by=user
        )
        member = services.accept_invitation(invitation=invitation, user=other_user)
        assert member.role == Role.MEMBER
        assert WorkspaceMember.objects.get(workspace=ws, user=other_user).accepted_at


class TestEmitsRideTheSameTransaction:
    """Every event this canon emits commits WITH the rows it describes.

    ``create_workspace`` carries its own ``@transaction.atomic``, so by the
    time it returns the workspace and the owner membership are committed —
    and the two follow-up emits in ``ensure_personal_workspace``
    (``workspace.personal.created``, ``workspace.member_joined``) were
    landing in autocommit, after the fact. Measured on a deployed client stand
    2026-09-12: every guest enrol logged two ``emit(...) called outside
    transaction.atomic()`` warnings from ``services.py`` lines 119 and 123.
    Detached outbox rows are the L2 bug stapel-core's guard exists to
    catch: die between the commit and the emit and the workspace exists
    while nothing downstream ever hears of it.

    The suite runs ``OUTBOX_ENABLED=False``, where ``emit`` returns before
    it ever looks at the connection — so core's own guard is structurally
    blind here and a test leaning on it would pass for the wrong reason.
    The assertion is made directly instead: wrap the name
    ``services`` actually calls and record ``in_atomic_block`` at each
    call. ``transaction=True`` is load-bearing — the default django_db
    fixture wraps the test itself in a transaction, under which every
    emit looks atomic no matter where it sits.
    """

    @pytest.fixture
    def atomicity(self, monkeypatch):
        from django.db import transaction as db_transaction

        from stapel_workspaces import services

        seen: list[tuple[str, bool]] = []
        real = services.emit

        def recording_emit(name, *args, **kwargs):
            seen.append(
                (name, db_transaction.get_connection().in_atomic_block)
            )
            return real(name, *args, **kwargs)

        monkeypatch.setattr(services, "emit", recording_emit)
        return seen

    @pytest.mark.django_db(transaction=True)
    def test_ensure_personal_workspace_emits_inside_a_transaction(self, user, atomicity):
        ws = ensure_personal_workspace(user)
        assert ws.type == WorkspaceType.PERSONAL
        assert WorkspaceMember.objects.filter(workspace=ws, user=user).exists()
        assert atomicity, "no emit was observed — the wrapper missed the call site"
        assert [name for name, atomic in atomicity if not atomic] == []

    @pytest.mark.django_db(transaction=True)
    def test_resolve_landing_workspace_emits_inside_a_transaction(self, user, atomicity):
        """The consumer's entry point, which is where the stand measured it."""
        ws = resolve_landing_workspace(user, origin="street")
        assert ws is not None
        assert atomicity
        assert [name for name, atomic in atomicity if not atomic] == []
