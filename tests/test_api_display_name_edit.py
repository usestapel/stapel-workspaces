"""Roster-side name correction: the two name-edit PATCHes (0.19.0).

``PATCH <ws>/members/<user_id>/name`` writes the CANONICAL display name —
stapel-profiles' ``Profile.display_name`` — and ``PATCH
<ws>/invitations/<id>/name`` writes the local ``display_name_hint`` of an
invitation nobody has accepted yet. Both are one mandate
(``members.role.change``) and one refusal vocabulary.

Two things these tests hold down that the implementation is easy to get
wrong in:

* **the name canon is stapel-profiles', not a second copy here.** The
  rejections must come out with that module's own
  ``error.400.display_name_*`` keys, produced by its own
  ``validate_display_name`` — the tests below call the real function
  through the real seam, they do not stand in a look-alike;
* **the seam is a NAME on the comm plane, never a symbol.** ``services``
  reaches profiles through ``call("profiles.set_display_name", ...)`` and
  its two companions, so these tests register stand-in providers under
  those names and nothing else — stapel_profiles is not in INSTALLED_APPS
  here, which is exactly the split topology 0.19.0's dotted-path seam
  could not serve. A run with the sibling genuinely mounted, real
  providers and all, lives in ``test_profiles_comounted.py``.
"""
import uuid

import pytest
from django.utils import timezone

from stapel_workspaces import services
from stapel_workspaces.errors import (
    ERR_400_DISPLAY_NAME_EMOJI,
    ERR_400_DISPLAY_NAME_FORBIDDEN_CHARS,
    ERR_400_DISPLAY_NAME_INVISIBLE_CHARS,
    ERR_400_DISPLAY_NAME_TOO_SHORT,
    ERR_400_INVITATION_ALREADY_USED,
    ERR_400_INVITATION_REVOKED,
    ERR_403_FORBIDDEN_WORKSPACE,
    ERR_403_MISSING_CAPABILITY,
    ERR_404_INVITATION_NOT_FOUND,
    ERR_404_MEMBER_NOT_FOUND,
    ERR_503_PROFILES_NOT_CONFIGURED,
    ERR_503_PROFILES_UNAVAILABLE,
)
from stapel_workspaces.models import Role, WorkspaceInvitation, WorkspaceMember

BASE = "/workspaces/api/workspaces/v1"



def _member_url(ws_id, user_id):
    return f"{BASE}/{ws_id}/members/{user_id}/name"


def _invitation_url(ws_id, invitation_id):
    return f"{BASE}/{ws_id}/invitations/{invitation_id}/name"


def _new_user(email=None):
    from stapel_core.django.users.models import User

    return User.objects.create_user(
        username=f"u-{uuid.uuid4().hex[:8]}",
        email=email or f"{uuid.uuid4().hex[:8]}@example.com",
        password="testpass-1234",
    )


def _create_ws(user, name="Acme"):
    return services.create_workspace(user=user, name=name)


def _add_member(ws, user, role):
    return WorkspaceMember.objects.create(
        workspace=ws, user=user, role=role, accepted_at=timezone.now()
    )


def _make_invitation(ws, email="invitee@example.com", **overrides):
    from datetime import timedelta

    fields = dict(
        workspace=ws,
        email=email,
        role=Role.MEMBER,
        token=uuid.uuid4().hex,
        expires_at=timezone.now() + timedelta(days=3),
    )
    fields.update(overrides)
    return WorkspaceInvitation.objects.create(**fields)


# --- the seam ------------------------------------------------------------
#
# stapel_profiles is NOT in INSTALLED_APPS for this file. That is the point:
# the only thing joining the two modules here is a function NAME on the comm
# plane, which is what makes the endpoint work in a split deployment. The
# stand-in providers below mirror stapel-profiles' real ones (0.10) — the
# structural {ok, reason} contract, the reason vocabulary — and delegate the
# one decision they must not fake, "what may a name contain", to that
# module's own validator.


@pytest.fixture(autouse=True)
def stapel_error_envelope(settings):
    """Render refusals the way a deployment renders them.

    The shared test harness deliberately ships no ``REST_FRAMEWORK`` block
    (see ``_codegen_settings``), so a serializer refusal comes out in DRF's
    raw ``{field: [detail]}`` shape rather than the StapelError envelope a
    real service returns. These tests are specifically about WHICH KEY
    reaches the client, so they install core's own exception handler for
    the duration — DRF reloads its settings on Django's ``setting_changed``
    signal, which is what makes a per-test override take.
    """
    settings.REST_FRAMEWORK = {
        "EXCEPTION_HANDLER": "stapel_core.django.api.errors.stapel_exception_handler",
    }


@pytest.fixture
def clean_error_registry():
    """Undo the global error-key registration profiles does when imported.

    Calling ``validate_display_name`` imports ``stapel_profiles.errors``,
    which registers that module's ~50 keys into the process-global service
    registry — the same registry this module's i18n catalog gate and
    error-key tests read. Restoring it keeps those gates measuring THIS
    module's contract rather than whatever a neighbouring test imported.
    """
    from stapel_core.django.api import errors as core_errors

    before = dict(core_errors._GLOBAL_REGISTRY)
    before_remediation = dict(core_errors._REMEDIATION_REGISTRY)
    yield
    core_errors._GLOBAL_REGISTRY.clear()
    core_errors._GLOBAL_REGISTRY.update(before)
    core_errors._REMEDIATION_REGISTRY.clear()
    core_errors._REMEDIATION_REGISTRY.update(before_remediation)


@pytest.fixture(autouse=True)
def clean_function_registry():
    """Leave the process-global comm registry exactly as it was found."""
    from stapel_core.comm.registry import function_registry

    providers = dict(function_registry._providers)
    schemas = dict(function_registry._schemas)
    yield
    function_registry._providers.clear()
    function_registry._providers.update(providers)
    function_registry._schemas.clear()
    function_registry._schemas.update(schemas)


def _drop_profiles_providers():
    from stapel_core.comm.registry import function_registry

    for name in (
        services.SET_DISPLAY_NAME,
        services.VALIDATE_DISPLAY_NAME,
        services.DISPLAY_NAMES,
    ):
        function_registry._providers.pop(name, None)
        function_registry._schemas.pop(name, None)


@pytest.fixture
def profiles_seam(clean_error_registry):
    """A deployment where profiles answers — in another process or this one.

    Registers the three providers stapel-profiles 0.10 publishes. The write
    stand-in stores names in a dict instead of a table, but it runs the
    REAL ``validate_display_name`` and returns the real structural contract,
    because those are the two things this module's behaviour is derived
    from. Where the row physically lands is that module's business and is
    covered by its own tests.
    """
    pytest.importorskip(
        "stapel_profiles.validators",
        reason="stapel-profiles is not installed; the display-name canon "
        "cannot be exercised without the module that owns it",
    )
    from stapel_core.comm.registry import function_registry
    from stapel_core.django.api.errors import StapelValidationError
    from stapel_profiles.validators import validate_display_name

    rows: dict = {}
    published: list = []
    calls: list = []

    def _reason(value):
        try:
            validate_display_name(value)
        except StapelValidationError as exc:
            return ".".join(exc.error_key.split(".")[2:])
        return None

    def _validate(payload):
        reason = _reason(payload["display_name"])
        return {"ok": reason is None, "reason": reason}

    def _set(payload):
        calls.append(payload)
        reason = _reason(payload["display_name"])
        if reason is not None:
            return {"ok": False, "display_name": None, "reason": reason}
        rows[str(payload["user_id"])] = payload["display_name"]
        published.append(payload["user_id"])
        return {"ok": True, "display_name": payload["display_name"], "reason": None}

    def _names(payload):
        return {
            "display_names": {
                uid: rows[uid]
                for uid in payload["user_ids"]
                if rows.get(uid)
            }
        }

    _drop_profiles_providers()
    function_registry.register(services.VALIDATE_DISPLAY_NAME, _validate)
    function_registry.register(services.SET_DISPLAY_NAME, _set)
    function_registry.register(services.DISPLAY_NAMES, _names)
    return {"rows": rows, "published": published, "calls": calls}


@pytest.fixture
def profiles_absent():
    """A deployment with no provider and no route: profiles is nowhere.

    Not "profiles is restarting" — nothing in this process or its
    configuration knows where profiles is, and no amount of waiting changes
    that. The endpoint must say so in those words.
    """
    _drop_profiles_providers()


@pytest.fixture
def profiles_unreachable():
    """A deployment wired to profiles, whose call fails right now."""
    from stapel_core.comm.registry import function_registry

    def _boom(payload):
        raise RuntimeError("connection reset by peer")

    _drop_profiles_providers()
    function_registry.register(services.SET_DISPLAY_NAME, _boom)
    function_registry.register(services.VALIDATE_DISPLAY_NAME, _boom)


# --- members/<user_id>/name ---------------------------------------------


@pytest.mark.django_db
class TestMemberNameEdit:
    def test_owner_renames_a_member(self, authed_client, user, other_user, profiles_seam):
        ws = _create_ws(user)
        _add_member(ws, other_user, Role.MEMBER)

        resp = authed_client.patch(
            _member_url(ws.id, other_user.id),
            {"display_name": "  Ada Lovelace  "},
            format="json",
        )

        assert resp.status_code == 200, resp.content
        assert resp.json() == {"display_name": "Ada Lovelace"}
        assert profiles_seam["rows"][str(other_user.id)] == "Ada Lovelace"

    def test_admin_renames_a_member(
        self, api_client, user, other_user, profiles_seam
    ):
        ws = _create_ws(user)
        admin = _new_user()
        _add_member(ws, admin, Role.ADMIN)
        _add_member(ws, other_user, Role.MEMBER)
        api_client.force_authenticate(user=admin)

        resp = api_client.patch(
            _member_url(ws.id, other_user.id),
            {"display_name": "Grace Hopper"},
            format="json",
        )

        assert resp.status_code == 200, resp.content
        assert profiles_seam["rows"][str(other_user.id)] == "Grace Hopper"

    def test_the_write_goes_out_as_the_named_operation(
        self, authed_client, user, other_user, profiles_seam
    ):
        """One call, one name, one payload — and nothing else crosses.

        This is the whole cutover in one assertion. Before 0.21.0 this
        endpoint reached INTO stapel-profiles — its validator, its model
        factory, its event publisher, three symbols by dotted path — which
        only worked where that module happened to be co-mounted. What
        crosses now is a name and two strings, and everything the name
        implies (the canon, the swappable model, get-or-create,
        ``profile.changed``) happens on the far side, in the module that
        owns it, whichever process that is.
        """
        ws = _create_ws(user)
        _add_member(ws, other_user, Role.MEMBER)

        authed_client.patch(
            _member_url(ws.id, other_user.id),
            {"display_name": "Ada Lovelace"},
            format="json",
        )

        assert profiles_seam["calls"] == [
            {"user_id": str(other_user.id), "display_name": "Ada Lovelace"}
        ]
        # And the far side announced the change — the rule profiles' own
        # llms.txt states for ANY write that skips its serializers. That it
        # happens is asserted here; that it CANNOT be forgotten is asserted
        # in stapel-profiles, where the emission now lives.
        assert profiles_seam["published"] == [str(other_user.id)]

    def test_blank_clears_the_name(
        self, authed_client, user, other_user, profiles_seam
    ):
        """"   ", "" and a missing key are one state: cleared.

        The canon's two-character minimum must not fire here — clearing a
        name is not a short name.
        """
        ws = _create_ws(user)
        _add_member(ws, other_user, Role.MEMBER)
        profiles_seam["rows"][str(other_user.id)] = "Old Name"

        resp = authed_client.patch(
            _member_url(ws.id, other_user.id), {"display_name": "   "}, format="json"
        )

        assert resp.status_code == 200, resp.content
        assert resp.json() == {"display_name": ""}
        assert profiles_seam["rows"][str(other_user.id)] == ""

    def test_missing_key_clears_the_name(
        self, authed_client, user, other_user, profiles_seam
    ):
        ws = _create_ws(user)
        _add_member(ws, other_user, Role.MEMBER)

        resp = authed_client.patch(_member_url(ws.id, other_user.id), {}, format="json")

        assert resp.status_code == 200, resp.content
        assert resp.json() == {"display_name": ""}

    def test_plain_member_is_refused(self, api_client, user, other_user, profiles_seam):
        ws = _create_ws(user)
        actor = _new_user()
        _add_member(ws, actor, Role.MEMBER)
        _add_member(ws, other_user, Role.MEMBER)
        api_client.force_authenticate(user=actor)

        resp = api_client.patch(
            _member_url(ws.id, other_user.id), {"display_name": "Nope"}, format="json"
        )

        assert resp.status_code == 403
        assert resp.json()["localizable_error"] == ERR_403_MISSING_CAPABILITY
        assert str(other_user.id) not in profiles_seam["rows"]

    def test_viewer_is_refused(self, api_client, user, other_user, profiles_seam):
        ws = _create_ws(user)
        actor = _new_user()
        _add_member(ws, actor, Role.VIEWER)
        _add_member(ws, other_user, Role.MEMBER)
        api_client.force_authenticate(user=actor)

        resp = api_client.patch(
            _member_url(ws.id, other_user.id), {"display_name": "Nope"}, format="json"
        )

        assert resp.status_code == 403

    def test_anonymous_is_refused(self, api_client, user, other_user):
        """The anonymous axis, declared ANONYMOUS_DENIED on the view."""
        ws = _create_ws(user)
        _add_member(ws, other_user, Role.MEMBER)

        resp = api_client.patch(
            _member_url(ws.id, other_user.id), {"display_name": "Nope"}, format="json"
        )

        assert resp.status_code in (401, 403)

    def test_an_admin_may_not_rename_the_owner(
        self, api_client, user, profiles_seam
    ):
        """Same hardcoded owner protection role changes and resets carry."""
        ws = _create_ws(user)
        admin = _new_user()
        _add_member(ws, admin, Role.ADMIN)
        api_client.force_authenticate(user=admin)

        resp = api_client.patch(
            _member_url(ws.id, user.id), {"display_name": "Not The Boss"}, format="json"
        )

        assert resp.status_code == 403
        assert resp.json()["localizable_error"] == ERR_403_FORBIDDEN_WORKSPACE
        assert str(user.id) not in profiles_seam["rows"]

    def test_an_owner_may_rename_another_owner(
        self, api_client, user, other_user, profiles_seam
    ):
        ws = _create_ws(user)
        _add_member(ws, other_user, Role.OWNER)
        api_client.force_authenticate(user=user)

        resp = api_client.patch(
            _member_url(ws.id, other_user.id), {"display_name": "Co Founder"},
            format="json",
        )

        assert resp.status_code == 200, resp.content

    def test_a_stranger_to_the_workspace_is_not_found(
        self, authed_client, user, other_user, profiles_seam
    ):
        ws = _create_ws(user)

        resp = authed_client.patch(
            _member_url(ws.id, other_user.id), {"display_name": "Nope"}, format="json"
        )

        assert resp.status_code == 404
        assert resp.json()["localizable_error"] == ERR_404_MEMBER_NOT_FOUND

    def test_admin_of_one_workspace_cannot_rename_a_member_of_another(
        self, api_client, user, other_user, profiles_seam
    ):
        """Cross-workspace isolation, both ways round.

        The capability is held in workspace A and asked for in workspace B,
        so the actor never gets past the mandate check there; and inside
        their OWN workspace the target is simply not a member, which is the
        same 404 an unknown UUID gets. Neither answer tells them anything
        about workspace B's roster.
        """
        ws_a = _create_ws(user, name="A")
        owner_b = _new_user()
        ws_b = _create_ws(owner_b, name="B")
        _add_member(ws_b, other_user, Role.MEMBER)
        api_client.force_authenticate(user=user)

        foreign = api_client.patch(
            _member_url(ws_b.id, other_user.id), {"display_name": "Nope"}, format="json"
        )
        assert foreign.status_code == 403
        assert foreign.json()["localizable_error"] == ERR_403_FORBIDDEN_WORKSPACE

        own = api_client.patch(
            _member_url(ws_a.id, other_user.id), {"display_name": "Nope"}, format="json"
        )
        assert own.status_code == 404

        assert str(other_user.id) not in profiles_seam["rows"]

    def test_over_long_name_is_the_column_ceiling_not_a_new_key(
        self, authed_client, user, other_user, profiles_seam
    ):
        """35 chars is a storage fact of both columns, so it is a field error.

        Deliberately NOT a freshly minted ``display_name_too_long``: the
        length ceiling is declared by the model and reported with the
        fleet-standard ``error.400.field.max_length`` carrying the field and
        the limit, which every frontend already renders.
        """
        ws = _create_ws(user)
        _add_member(ws, other_user, Role.MEMBER)

        resp = authed_client.patch(
            _member_url(ws.id, other_user.id), {"display_name": "x" * 36},
            format="json",
        )

        assert resp.status_code == 400
        assert resp.json()["localizable_error"] == "error.400.field.max_length"
        assert str(other_user.id) not in profiles_seam["rows"]

    def test_the_split_deployment_that_used_to_be_broken_now_works(
        self, authed_client, user, other_user, profiles_seam
    ):
        """The defect this release exists to remove.

        stapel_profiles is not in this process — ``profiles_seam`` registers
        nothing but three names on the comm plane, which is what a route to
        another container looks like from here. 0.19.0 answered 503 forever
        in exactly this shape, because its seam asked the app registry first
        and gave up when the sibling was not local.
        """
        from django.apps import apps as django_apps

        assert not django_apps.is_installed("stapel_profiles"), (
            "this test is only meaningful when profiles runs elsewhere"
        )
        ws = _create_ws(user)
        _add_member(ws, other_user, Role.MEMBER)

        resp = authed_client.patch(
            _member_url(ws.id, other_user.id), {"display_name": "Ada Lovelace"},
            format="json",
        )

        assert resp.status_code == 200, resp.content
        assert resp.json() == {"display_name": "Ada Lovelace"}
        assert profiles_seam["rows"][str(other_user.id)] == "Ada Lovelace"

    def test_no_provider_and_no_route_is_a_configuration_refusal(
        self, authed_client, user, other_user, profiles_absent
    ):
        """Nowhere to write = 503, and one that names its own cause.

        Never a 200 over a write that did not happen, and never a silent
        fallback onto ``WorkspaceMember.display_name_hint`` — that column
        goes dark the moment a profile exists, so a "correction" written
        there is one the renamed person never sees.

        The key is ``profiles_not_configured``, whose remediation is
        ``contact_support``. 0.19.0 answered ``profiles_unavailable`` /
        ``wait_and_retry`` here — advising the caller to wait for a module
        that was never coming. An unconfigured comm route is a
        configuration fact: only an operator resolves it, and it never
        resolves itself (env-address-class v2 §2).
        """
        ws = _create_ws(user)
        _add_member(ws, other_user, Role.MEMBER)

        resp = authed_client.patch(
            _member_url(ws.id, other_user.id), {"display_name": "Ada Lovelace"},
            format="json",
        )

        assert resp.status_code == 503
        assert resp.json()["localizable_error"] == ERR_503_PROFILES_NOT_CONFIGURED

    def test_the_configuration_refusal_does_not_advise_waiting(self):
        """The remediation split, asserted where it is declared."""
        from stapel_workspaces.errors import WORKSPACES_REMEDIATION

        assert WORKSPACES_REMEDIATION[ERR_503_PROFILES_NOT_CONFIGURED] == (
            "contact_support"
        )
        # The transient sibling keeps the hint that is true for IT.
        assert WORKSPACES_REMEDIATION[ERR_503_PROFILES_UNAVAILABLE] == "wait_and_retry"

    def test_a_wired_but_failing_profiles_is_the_transient_503(
        self, authed_client, user, other_user, profiles_unreachable
    ):
        """Routed, called, failed — this one really is "try again later"."""
        ws = _create_ws(user)
        _add_member(ws, other_user, Role.MEMBER)

        resp = authed_client.patch(
            _member_url(ws.id, other_user.id), {"display_name": "Ada Lovelace"},
            format="json",
        )

        assert resp.status_code == 503
        assert resp.json()["localizable_error"] == ERR_503_PROFILES_UNAVAILABLE


@pytest.mark.django_db
class TestTheNameCanonIsProfiles:
    """The rejections must be stapel-profiles' own, key for key.

    Each case below is a rule this module deliberately does NOT own a copy
    of. If any of them starts answering with a workspaces-minted key, the
    second name validator the fleet keeps fighting has grown back.
    """

    @pytest.mark.parametrize(
        "name,expected",
        [
            ("x", ERR_400_DISPLAY_NAME_TOO_SHORT),
            ("Ada <script>", ERR_400_DISPLAY_NAME_FORBIDDEN_CHARS),
            ("Ada​Lovelace", ERR_400_DISPLAY_NAME_INVISIBLE_CHARS),
            ("Ada \U0001f600", ERR_400_DISPLAY_NAME_EMOJI),
        ],
    )
    def test_member_rename_surfaces_the_library_key(
        self, authed_client, user, other_user, profiles_seam, name, expected
    ):
        ws = _create_ws(user)
        _add_member(ws, other_user, Role.MEMBER)

        resp = authed_client.patch(
            _member_url(ws.id, other_user.id), {"display_name": name}, format="json"
        )

        assert resp.status_code == 400, resp.content
        assert resp.json()["localizable_error"] == expected
        assert str(other_user.id) not in profiles_seam["rows"]

    @pytest.mark.parametrize(
        "name,expected",
        [
            ("x", ERR_400_DISPLAY_NAME_TOO_SHORT),
            ("Ada <script>", ERR_400_DISPLAY_NAME_FORBIDDEN_CHARS),
            ("Ada​Lovelace", ERR_400_DISPLAY_NAME_INVISIBLE_CHARS),
            ("Ada \U0001f600", ERR_400_DISPLAY_NAME_EMOJI),
        ],
    )
    def test_invitation_rename_surfaces_the_same_key(
        self, authed_client, user, profiles_seam, name, expected
    ):
        """One field, one vocabulary — before and after acceptance."""
        ws = _create_ws(user)
        inv = _make_invitation(ws)

        resp = authed_client.patch(
            _invitation_url(ws.id, inv.id), {"display_name": name}, format="json"
        )

        assert resp.status_code == 400, resp.content
        assert resp.json()["localizable_error"] == expected
        inv.refresh_from_db()
        assert inv.display_name_hint == ""


# --- invitations/<invitation_id>/name ------------------------------------


@pytest.mark.django_db
class TestInvitationNameEdit:
    def test_owner_edits_the_hint(self, authed_client, user, profiles_seam):
        ws = _create_ws(user)
        inv = _make_invitation(ws)

        resp = authed_client.patch(
            _invitation_url(ws.id, inv.id),
            {"display_name": "  Marie Curie  "},
            format="json",
        )

        assert resp.status_code == 200, resp.content
        assert resp.json() == {"display_name": "Marie Curie"}
        inv.refresh_from_db()
        assert inv.display_name_hint == "Marie Curie"

    def test_admin_edits_the_hint(self, api_client, user, profiles_seam):
        ws = _create_ws(user)
        admin = _new_user()
        _add_member(ws, admin, Role.ADMIN)
        inv = _make_invitation(ws)
        api_client.force_authenticate(user=admin)

        resp = api_client.patch(
            _invitation_url(ws.id, inv.id), {"display_name": "Alan Turing"},
            format="json",
        )

        assert resp.status_code == 200, resp.content
        inv.refresh_from_db()
        assert inv.display_name_hint == "Alan Turing"

    def test_blank_clears_the_hint_to_empty_string_not_null(
        self, authed_client, user, profiles_seam
    ):
        ws = _create_ws(user)
        inv = _make_invitation(ws, display_name_hint="Typo")

        resp = authed_client.patch(
            _invitation_url(ws.id, inv.id), {"display_name": ""}, format="json"
        )

        assert resp.status_code == 200, resp.content
        inv.refresh_from_db()
        assert inv.display_name_hint == ""

    def test_it_works_without_profiles_in_the_process(
        self, authed_client, user, profiles_absent
    ):
        """The hint is a LOCAL column — nothing to reach out for.

        Unlike the member endpoint, this write has no cross-module target,
        so a deployment that does not ship stapel-profiles can still fix a
        pending invite's name. What it loses in that case is the canon (no
        module owns one there), leaving exactly the rule this module already
        applied to the same field at invite time: the column ceiling.
        """
        ws = _create_ws(user)
        inv = _make_invitation(ws)

        resp = authed_client.patch(
            _invitation_url(ws.id, inv.id), {"display_name": "Marie Curie"},
            format="json",
        )

        assert resp.status_code == 200, resp.content
        inv.refresh_from_db()
        assert inv.display_name_hint == "Marie Curie"

    def test_plain_member_is_refused(self, api_client, user, profiles_seam):
        ws = _create_ws(user)
        actor = _new_user()
        _add_member(ws, actor, Role.MEMBER)
        inv = _make_invitation(ws)
        api_client.force_authenticate(user=actor)

        resp = api_client.patch(
            _invitation_url(ws.id, inv.id), {"display_name": "Nope"}, format="json"
        )

        assert resp.status_code == 403
        assert resp.json()["localizable_error"] == ERR_403_MISSING_CAPABILITY
        inv.refresh_from_db()
        assert inv.display_name_hint == ""

    def test_anonymous_is_refused(self, api_client, user):
        ws = _create_ws(user)
        inv = _make_invitation(ws)

        resp = api_client.patch(
            _invitation_url(ws.id, inv.id), {"display_name": "Nope"}, format="json"
        )

        assert resp.status_code in (401, 403)

    def test_revoked_invitation_is_not_editable(
        self, authed_client, user, profiles_seam
    ):
        ws = _create_ws(user)
        inv = _make_invitation(ws, revoked_at=timezone.now())

        resp = authed_client.patch(
            _invitation_url(ws.id, inv.id), {"display_name": "Nope"}, format="json"
        )

        assert resp.status_code == 400
        assert resp.json()["localizable_error"] == ERR_400_INVITATION_REVOKED

    def test_accepted_invitation_is_not_editable(
        self, authed_client, user, profiles_seam
    ):
        """Its name is the member's name now — use the member endpoint."""
        ws = _create_ws(user)
        inv = _make_invitation(ws, accepted_at=timezone.now())

        resp = authed_client.patch(
            _invitation_url(ws.id, inv.id), {"display_name": "Nope"}, format="json"
        )

        assert resp.status_code == 400
        assert resp.json()["localizable_error"] == ERR_400_INVITATION_ALREADY_USED

    def test_unknown_invitation_is_not_found(self, authed_client, user, profiles_seam):
        ws = _create_ws(user)

        resp = authed_client.patch(
            _invitation_url(ws.id, uuid.uuid4()), {"display_name": "Nope"},
            format="json",
        )

        assert resp.status_code == 404
        assert resp.json()["localizable_error"] == ERR_404_INVITATION_NOT_FOUND

    def test_an_invitation_of_another_workspace_is_the_same_404(
        self, api_client, user, profiles_seam
    ):
        """Cross-workspace isolation: identical to an id that never existed."""
        ws_a = _create_ws(user, name="A")
        owner_b = _new_user()
        ws_b = _create_ws(owner_b, name="B")
        inv_b = _make_invitation(ws_b, email="pending-b@example.com")
        api_client.force_authenticate(user=user)

        resp = api_client.patch(
            _invitation_url(ws_a.id, inv_b.id), {"display_name": "Nope"}, format="json"
        )

        assert resp.status_code == 404
        assert resp.json()["localizable_error"] == ERR_404_INVITATION_NOT_FOUND
        inv_b.refresh_from_db()
        assert inv_b.display_name_hint == ""

    def test_over_long_hint_is_the_column_ceiling(
        self, authed_client, user, profiles_seam
    ):
        ws = _create_ws(user)
        inv = _make_invitation(ws)

        resp = authed_client.patch(
            _invitation_url(ws.id, inv.id), {"display_name": "x" * 36}, format="json"
        )

        assert resp.status_code == 400
        assert resp.json()["localizable_error"] == "error.400.field.max_length"
        inv.refresh_from_db()
        assert inv.display_name_hint == ""
