"""The lifecycle predicates: one spelling, and a rule that keeps it one.

`accepted_at IS NOT NULL AND suspended_at IS NULL` used to be hand-written
at nine call sites, and only some of them knew about suspension. That is not
a bug, it is a bug *factory*: every new lifecycle column silently
invalidates a subset of the copies, and nothing fails. One instance already
cost money — the seat count billed suspended members (#92).

Two things are pinned here.

**The rule.** :class:`TestNoRawLifecycleFilters` walks the package's AST and
fails if ``accepted_at__isnull`` / ``suspended_at__isnull`` /
``declined_at__isnull`` / ``revoked_at__isnull`` appear in a query call
anywhere outside ``models.py``. A new spelling reddens the suite with the
offender's file and line.

**The predicates themselves.** :class:`TestPredicatePerCallSite` walks every
place that used to carry a copy and asserts what that place now selects. The
table (place → predicate → did the answer change):

===========================================  =====================  =======
place                                        predicate              changed
===========================================  =====================  =======
``functions.check_membership``               ``active()``           no
``services.enforce_require_mfa``             ``active()``           no
``services.suspend_memberships_without_mfa`` ``active()``           no
``services.suspend_..._deactivated_user``    ``active()``           no
``services.lift_*`` (x3)                     ``suspended(reason)``  no
``permissions.get_membership``               ``active()``           no
``permissions.get_membership(incl.susp.)``   ``accepted()``         no
``views.InternalMembershipView``             ``active()``           no
``views.WorkspaceListCreateView.get``        ``active()``           no
``entitlements`` (member half)               ``holds_seat()``       no
``views._workspace_to_dto`` member_count     ``active()``           **YES**
``entitlements`` (invitation half)           ``pending()``          **YES**
``services.accept_invitation`` lock          ``unresolved()``       **YES**
``gdpr.delete`` (invitations)                ``never_accepted()``   no
``gdpr.anonymize`` (invitations)             ``accepted()``         no
===========================================  =====================  =======

Note the last two: they were counted among the "nine spellings of active
membership" and are neither — they filter INVITATIONS. Same column name,
different model, different question. Folding them into the membership
predicate would have been a silent behaviour change.

**What this file does not check.** Nothing here knows whether ``active()``
matches the owner's definition of an active user (registered AND activated
this month; never-signed-in excluded). It does not — ``last_accessed_at`` is
never consulted, and a live invitation reserves a seat for someone who has
never signed in. The rule prevents the *second* drift, between copies, once
a human has translated the spec into columns; the *first*, semantic
divergence needs a spec-derived test or an end-to-end scenario, and there is
none in this repository.
"""

import ast
import pathlib
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from django.test import override_settings
from django.utils import timezone

import stapel_workspaces
from stapel_workspaces import entitlements, services
from stapel_workspaces.gdpr import WorkspacesGDPRProvider
from stapel_workspaces.models import (
    SUSPENSION_ACCOUNT_DEACTIVATED,
    SUSPENSION_NO_MFA,
    Role,
    WorkspaceInvitation,
    WorkspaceMember,
)
from stapel_workspaces.permissions import get_membership

from stapel_core.comm.registry import function_registry

BASE = "/workspaces/api/workspaces/v1"
SERVICE_KEY = "test-service-key"

service_settings = override_settings(
    MIDDLEWARE=["stapel_core.django.jwt.middleware.ServiceAPIKeyMiddleware"],
    SERVICE_API_KEY=SERVICE_KEY,
)


# --------------------------------------------------------------------------
# The rule: raw lifecycle columns may be spelled only in models.py
# --------------------------------------------------------------------------

#: Query kwargs that must not be written by hand outside ``models.py``.
#: An explicit list, not a heuristic: a rule that guesses which columns are
#: "lifecycle" produces false positives, and a rule people mute is worse
#: than no rule. Adding a lifecycle column means adding it here AND giving
#: it a named predicate.
BANNED_LOOKUPS = frozenset(
    {
        "accepted_at__isnull",
        "suspended_at__isnull",
        "declined_at__isnull",
        "revoked_at__isnull",
    }
)

#: Call names treated as queries. ``Q`` is included because a lifecycle
#: clause hidden in a Q object drifts exactly like one in filter().
QUERY_CALLS = frozenset(
    {"filter", "exclude", "get", "get_or_create", "update_or_create", "Q"}
)

PACKAGE_ROOT = Path(stapel_workspaces.__file__).resolve().parent
SKIPPED_DIRS = {"tests", "migrations", "build", "__pycache__", "schemas"}


def raw_lifecycle_filters(source: str, filename: str) -> list[str]:
    """Return ``file:line: kwarg`` for every hand-written lifecycle lookup."""
    hits = []
    for node in ast.walk(ast.parse(source, filename=filename)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = (
            func.attr
            if isinstance(func, ast.Attribute)
            else func.id if isinstance(func, ast.Name) else None
        )
        if name not in QUERY_CALLS:
            continue
        for kw in node.keywords:
            if kw.arg in BANNED_LOOKUPS:
                hits.append(f"{filename}:{node.lineno}: {kw.arg}")
    return hits


def _venv_roots():
    """Directories that are virtualenvs, found by their defining marker.

    A venv created inside the repo (``.venv``, ``venv``, ``.direnv/...``)
    contains every INSTALLED sibling library, and those are not this
    package's sources. Naming the marker rather than the directory is the
    point: a name list goes stale the moment someone uses a name nobody
    thought of, and the rule then reports a sibling library's file as this
    repo's violation — a gate accusing the wrong file is worse than no gate,
    because the reader chases a defect that is not there.
    """
    return {cfg.parent for cfg in PACKAGE_ROOT.rglob("pyvenv.cfg")}


def _is_foreign_source(path, venvs) -> bool:
    """True when *path* is not this package's own source.

    Split out from the walk so the exclusion can be asserted on synthetic
    paths: in CI there is no in-repo venv, so a test that only walked the
    real tree would pass vacuously and the guard would rot unnoticed.
    """
    if any(venv in path.parents for venv in venvs):
        return True
    if "site-packages" in path.parts:
        return True  # a vendored tree without a pyvenv.cfg
    return False


def _package_sources():
    venvs = _venv_roots()
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        if _is_foreign_source(path, venvs):
            continue
        if SKIPPED_DIRS & set(path.relative_to(PACKAGE_ROOT).parts):
            continue
        if path.name == "models.py":
            continue  # the one sanctioned home of these columns
        yield path


class TestNoRawLifecycleFilters:
    def test_installed_siblings_are_not_this_package(self):
        """A venv inside the repo must not be read as this repo's source.

        Without this, `pip install -e .` into an in-repo `.venv` makes the
        rule report e.g. stapel-core's `gateway/tokens.py` as a workspaces
        violation — a real red on a file this repo does not own, which sends
        the reader hunting a defect that is not there.
        """
        # A synthetic root, NOT PACKAGE_ROOT: when the package is installed
        # non-editable, PACKAGE_ROOT is itself under site-packages, and the
        # "ours" path built from it would be flagged by the very rule under
        # test. The assertions are about path SHAPE, so they must not depend
        # on how this checkout happens to be installed.
        root = pathlib.Path("/synthetic/repo")
        venv = root / ".venv"
        installed = venv / "lib" / "python3.12" / "site-packages" / "sibling.py"
        vendored = root / "vendor" / "site-packages" / "other.py"
        ours = root / "services.py"

        assert _is_foreign_source(installed, {venv})
        assert _is_foreign_source(vendored, set())
        assert not _is_foreign_source(ours, {venv})

    def test_venv_roots_are_found_by_their_marker(self, tmp_path):
        """The marker, not the directory name, is what identifies a venv."""
        for name in (".venv", "venv", "env-3.12"):
            (tmp_path / name).mkdir()
            (tmp_path / name / "pyvenv.cfg").write_text("home = /usr\n")
        found = {cfg.parent for cfg in tmp_path.rglob("pyvenv.cfg")}
        assert found == {tmp_path / n for n in (".venv", "venv", "env-3.12")}

    def test_detector_flags_a_raw_spelling(self):
        """The detector is not vacuous — it catches a planted violation.

        Without this, a typo in the walker would leave the rule green
        forever, which is the failure mode the rule exists to prevent.
        """
        planted = (
            "def f(qs):\n"
            "    return qs.filter(accepted_at__isnull=False,"
            " suspended_at__isnull=True)\n"
        )
        assert raw_lifecycle_filters(planted, "planted.py") == [
            "planted.py:2: accepted_at__isnull",
            "planted.py:2: suspended_at__isnull",
        ]

    def test_detector_accepts_the_named_predicate(self):
        clean = "def f(qs):\n    return qs.active().filter(user_id=1)\n"
        assert raw_lifecycle_filters(clean, "clean.py") == []

    def test_models_still_owns_the_columns(self):
        """Guards against the rule going vacuous by renaming.

        If the lifecycle columns are renamed and ``BANNED_LOOKUPS`` is not
        updated, every other assertion in this class passes trivially.
        """
        source = (PACKAGE_ROOT / "models.py").read_text()
        assert raw_lifecycle_filters(source, "models.py"), (
            "models.py no longer spells the lifecycle columns — either the "
            "predicates moved or the columns were renamed; update "
            "BANNED_LOOKUPS or this rule now guards nothing."
        )

    def test_no_raw_lifecycle_filters_outside_models(self):
        violations = []
        for path in _package_sources():
            violations += raw_lifecycle_filters(
                path.read_text(), str(path.relative_to(PACKAGE_ROOT))
            )
        assert not violations, (
            "hand-written lifecycle predicate(s) outside models.py:\n  "
            + "\n  ".join(violations)
            + "\n\nUse the named querysets instead — MembershipQuerySet"
            " (.active() / .accepted() / .suspended() / .holds_seat()) or"
            " InvitationQuerySet (.pending() / .unresolved() / .accepted()"
            " / .never_accepted()). Spelling the columns by hand is how the"
            " nine copies stopped agreeing."
        )


# --------------------------------------------------------------------------
# The predicates: what each former call site now selects
# --------------------------------------------------------------------------


def _user():
    from stapel_core.django.users.models import User

    return User.objects.create_user(
        username=f"u-{uuid.uuid4().hex[:8]}",
        email=f"{uuid.uuid4().hex[:8]}@example.com",
        password="testpass-1234",
    )


def _member(ws, user, role=Role.MEMBER, **kwargs):
    kwargs.setdefault("accepted_at", timezone.now())
    return WorkspaceMember.objects.create(
        workspace=ws, user=user, role=role, **kwargs
    )


def _invitation(ws, inviter, email, **kwargs):
    kwargs.setdefault("expires_at", timezone.now() + timedelta(days=7))
    return WorkspaceInvitation.objects.create(
        workspace=ws,
        email=email,
        role=Role.MEMBER,
        invited_by=inviter,
        token=uuid.uuid4().hex,
        **kwargs,
    )


class Org:
    """One workspace holding every lifecycle state at once."""


@pytest.fixture
def org(db):
    o = Org()
    o.owner = _user()
    o.ws = services.create_workspace(user=o.owner, name="Acme")
    o.active = _user()
    o.suspended = _user()
    o.invited = _user()
    _member(o.ws, o.active)
    _member(
        o.ws,
        o.suspended,
        suspended_at=timezone.now(),
        suspension_reason=SUSPENSION_NO_MFA,
    )
    # A membership row that was never taken up (MemberState.INVITED).
    _member(o.ws, o.invited, accepted_at=None)
    # Invitations in every state.
    o.inv_live = _invitation(o.ws, o.owner, "live@example.com")
    o.inv_declined = _invitation(
        o.ws, o.owner, "declined@example.com", declined_at=timezone.now()
    )
    o.inv_revoked = _invitation(
        o.ws, o.owner, "revoked@example.com", revoked_at=timezone.now()
    )
    o.inv_expired = _invitation(
        o.ws,
        o.owner,
        "expired@example.com",
        expires_at=timezone.now() - timedelta(days=1),
    )
    o.inv_accepted = _invitation(
        o.ws, o.owner, "accepted@example.com", accepted_at=timezone.now()
    )
    return o


@pytest.fixture
def fake_mfa_status():
    """Scripted ``auth.mfa_status`` provider; records who was asked."""
    state = {"asked": [], "strong": set()}

    def provider(payload):
        state["asked"].append(payload["user_id"])
        return {"has_strong_mfa": payload["user_id"] in state["strong"]}

    function_registry.register(services.MFA_STATUS, provider)
    yield state
    function_registry._providers.pop(services.MFA_STATUS, None)
    function_registry._schemas.pop(services.MFA_STATUS, None)


@pytest.mark.django_db
class TestPredicatePerCallSite:
    """One test per former hand-written copy. Unchanged places included:
    a refactor that quietly moved one of them is the thing to catch."""

    # --- active(): unchanged behaviour ---------------------------------

    def test_check_membership_function(self, org):
        from stapel_workspaces.functions import check_membership

        def is_member(u):
            return check_membership(
                {"workspace_id": str(org.ws.id), "user_id": str(u.id)}
            )["is_member"]

        assert is_member(org.active) is True
        assert is_member(org.suspended) is False
        assert is_member(org.invited) is False

    def test_get_membership_and_include_suspended(self, org):
        assert get_membership(org.ws.id, org.active.id) is not None
        assert get_membership(org.ws.id, org.suspended.id) is None
        assert get_membership(org.ws.id, org.invited.id) is None
        # accepted(): the only caller allowed to see a suspended row.
        seen = get_membership(
            org.ws.id, org.suspended.id, include_suspended=True
        )
        assert seen is not None and seen.suspended_at is not None
        # ...and it still does not resurrect a never-accepted one.
        assert (
            get_membership(org.ws.id, org.invited.id, include_suspended=True)
            is None
        )

    def test_enforce_require_mfa_sweeps_only_active(self, org, fake_mfa_status):
        fake_mfa_status["strong"] = {str(org.owner.id), str(org.active.id)}
        # The sweep answers with its enforcement record (WORK-01): "every
        # active member was asked and answered" is a state, not a boolean.
        assert services.enforce_require_mfa(org.ws).state == "enforced"
        assert sorted(fake_mfa_status["asked"]) == sorted(
            [str(org.owner.id), str(org.active.id)]
        )

    def test_mfa_sweep_does_not_touch_already_suspended(
        self, org, fake_mfa_status
    ):
        before = WorkspaceMember.objects.get(
            workspace=org.ws, user=org.suspended
        ).suspended_at
        services.enforce_require_mfa(org.ws)
        after = WorkspaceMember.objects.get(
            workspace=org.ws, user=org.suspended
        )
        assert after.suspended_at == before
        assert after.suspension_reason == SUSPENSION_NO_MFA

    def test_suspend_memberships_without_mfa_only_active(self, org, monkeypatch):
        monkeypatch.setattr(
            services,
            "security_settings_for",
            lambda ws: type("S", (), {"require_mfa": True})(),
        )
        assert services.suspend_memberships_without_mfa(org.suspended.id) == 0
        assert services.suspend_memberships_without_mfa(org.active.id) == 1
        assert services.suspend_memberships_without_mfa(org.invited.id) == 0

    def test_suspend_memberships_for_deactivated_user_only_active(self, org):
        fn = services.suspend_memberships_for_deactivated_user
        assert fn(org.suspended.id) == 0  # idempotent, reason preserved
        assert (
            WorkspaceMember.objects.get(
                workspace=org.ws, user=org.suspended
            ).suspension_reason
            == SUSPENSION_NO_MFA
        )
        assert fn(org.active.id) == 1
        assert fn(org.invited.id) == 0

    def test_lifts_are_scoped_to_their_own_reason(self, org):
        """``suspended(reason=...)``: each consumer lifts only what it set."""
        assert (
            services.lift_deactivation_suspensions_for_user(org.suspended.id) == 0
        )
        assert services.lift_no_mfa_suspensions_for_user(org.suspended.id) == 1

        deactivated = _user()
        _member(
            org.ws,
            deactivated,
            suspended_at=timezone.now(),
            suspension_reason=SUSPENSION_ACCOUNT_DEACTIVATED,
        )
        assert services.lift_no_mfa_suspensions(org.ws) == 0
        assert (
            services.lift_deactivation_suspensions_for_user(deactivated.id) == 1
        )

    def test_workspace_list_hides_suspended_membership(self, org, api_client):
        api_client.force_authenticate(user=org.suspended)
        assert api_client.get(f"{BASE}/").json()["workspaces"] == []
        api_client.force_authenticate(user=org.active)
        listed = api_client.get(f"{BASE}/").json()["workspaces"]
        assert [w["id"] for w in listed] == [str(org.ws.id)]

    @service_settings
    def test_internal_membership_endpoint(self, org, api_client):
        def status_for(u):
            return api_client.get(
                f"{BASE}/internal/{org.ws.id}/members/{u.id}",
                HTTP_X_API_KEY=SERVICE_KEY,
            ).status_code

        assert status_for(org.active) == 200
        assert status_for(org.suspended) == 404
        assert status_for(org.invited) == 404

    def test_seat_count_excludes_suspended_and_never_accepted(self, org):
        """holds_seat(): the #92 fix, re-pinned at the predicate."""
        seated = org.ws.members.holds_seat()
        assert {m.user_id for m in seated} == {org.owner.id, org.active.id}

    # --- CHANGED behaviour ---------------------------------------------

    def test_member_count_no_longer_counts_suspended(self, org, api_client):
        """CHANGED (0.10.0): the workspace card counted suspended members.

        Was accepted() — 3 here (owner + active + suspended). Now active().
        """
        api_client.force_authenticate(user=org.owner)
        body = api_client.get(f"{BASE}/{org.ws.id}").json()
        assert body["member_count"] == 2

    def test_declined_invitation_no_longer_holds_a_seat(self, org):
        """CHANGED (0.10.0): money. The invitation half of the seat count
        knew about revocation and the TTL but not about ``declined_at``, so
        an invitation the invitee had refused kept a paid seat reserved
        until it expired.

        Seats here: owner + active member + the one live invitation = 3
        (was 4, the declined one).
        """
        assert {i.email for i in org.ws.invitations.pending()} == {
            "live@example.com"
        }
        assert entitlements.member_seats_quantity(org.ws) == 3
        assert entitlements.member_seats_quantity(org.ws, additional=2) == 5

    def test_revoked_invitation_cannot_be_accepted(self, org):
        """CHANGED (0.10.0): the accept row-lock had lost the revoked_at
        clause that decline's kept, so a revocation committing between the
        view's state check and the lock lost the race.
        """
        invitee = _user()
        with pytest.raises(ValueError):
            services.accept_invitation(invitation=org.inv_revoked, user=invitee)
        assert not WorkspaceMember.objects.filter(
            workspace=org.ws, user=invitee
        ).exists()

    def test_declined_invitation_still_cannot_be_accepted(self, org):
        invitee = _user()
        with pytest.raises(ValueError):
            services.accept_invitation(invitation=org.inv_declined, user=invitee)

    # --- the two that are NOT membership predicates ---------------------

    def test_gdpr_delete_removes_every_never_accepted_invitation(self, org):
        """never_accepted(), not pending(): erasure asks "did this ever
        become a membership", so declined/revoked/expired rows go too."""
        WorkspacesGDPRProvider().delete(org.owner.id)
        left = set(
            WorkspaceInvitation.objects.filter(
                invited_by_id=org.owner.id
            ).values_list("email", flat=True)
        )
        assert left == {"accepted@example.com"}

    def test_gdpr_anonymize_touches_only_accepted_invitations(self, org):
        WorkspacesGDPRProvider().anonymize(org.owner.id)
        org.inv_accepted.refresh_from_db()
        org.inv_declined.refresh_from_db()
        assert org.inv_accepted.invited_by_id is None
        assert org.inv_declined.invited_by_id == org.owner.id
