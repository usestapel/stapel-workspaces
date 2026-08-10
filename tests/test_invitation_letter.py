"""Which letter an invited address actually receives — and a gate on the rest.

Two things live here, and the second is the more valuable one.

**The branch.** ``workspace.invitation.new_user`` is the invitation letter
for an address with no account yet: its copy says the button creates the
account and joins the workspace, which is the only honest sentence for a
stranger and a lie to somebody who already has a login. The type, its
template, its routing entry and its six translation keys all shipped in
stapel-notifications 0.6.1 — and nothing in this module ever selected it.
For two minor versions every invitee, account or no account, got the
has-an-account letter. :class:`TestWhichLetterTheInviteeGets` pins the
branch to the observable outcome: the type requested, and the template that
type resolves to in the real catalog.

**The class.** That defect is not "a missing if". It is the fleet's
most-repeated shape: a mechanism was built, its consumer never picked it
up, and no test could notice because every test asserted what the code
does rather than what the catalog offers.
:class:`TestEveryWorkspaceLetterIsReachable` is the mechanism for the whole
seam rather than the one case — it reads the notification catalog
stapel-notifications actually ships, extracts the notification types this
package can actually request (statically, from the AST, resolving the
``notification_type`` parameter through its default and through every call
site), and fails when the two sets differ in EITHER direction:

* a ``workspace.*`` type in the catalog that this module never requests is
  a letter nobody can receive — exactly the ``.new_user`` defect, and it
  would have failed this test on the day the type landed;
* a type this module requests that the catalog does not carry is the same
  bug mirrored — ``request_notification`` logs an unknown type and drops
  it, so the letter silently never goes out.

The catalog is read from the installed sibling, never from a copy kept
here: a second list of the types would rot in precisely the way this test
exists to detect. CI installs stapel-notifications as a test-only sibling
(``--no-deps``, the same shape as stapel-profiles) so the gate is real
there; without it, the class skips.
"""
import ast
import pathlib

import pytest
from django.utils import timezone

from stapel_workspaces import services
from stapel_workspaces.models import Role

PACKAGE = pathlib.Path(services.__file__).resolve().parent

#: The catalog namespace this module owns. Every letter about a workspace,
#: its invitations and its members is requested from here and nowhere else,
#: so within this prefix "in the catalog" and "reachable from this package"
#: must be the same set.
PREFIX = "workspace."

notifications = pytest.importorskip(
    "stapel_notifications.routing",
    reason="stapel-notifications is not installed — the letter catalog it "
    "owns cannot be read (CI installs it --no-deps as a test-only sibling)",
)


# ---------------------------------------------------------------------------
# The branch: a stranger and an account holder get different letters
# ---------------------------------------------------------------------------


@pytest.fixture
def letters(monkeypatch):
    """Capture what this module hands to ``request_notification``."""
    sent = []
    monkeypatch.setattr(
        "stapel_core.notifications.request_notification",
        lambda notification_type, **kwargs: sent.append((notification_type, kwargs)),
    )
    return sent


def _template_of(notification_type: str) -> str:
    """The email template the REAL catalog resolves this type to."""
    return notifications.get_email_template(notification_type)


@pytest.mark.django_db
class TestWhichLetterTheInviteeGets:
    def test_an_address_with_no_account_gets_the_new_user_letter(
        self, user, letters
    ):
        """The whole defect, stated as the outcome a person sees.

        Nobody holds this address, so the accept link has to create the
        account first — and the letter has to say so.
        """
        ws = services.create_workspace(user=user, name="Acme")
        services.create_invitation(
            workspace=ws,
            email="stranger@example.com",
            role=Role.MEMBER,
            invited_by=user,
        )

        assert [t for t, _ in letters] == [services.NOTIFICATION_INVITATION_NEW_USER]
        assert (
            _template_of(letters[0][0])
            == "notifications/email/workspace_invitation_new_user.html"
        )

    def test_an_address_with_an_account_gets_the_plain_letter(
        self, user, other_user, letters
    ):
        ws = services.create_workspace(user=user, name="Acme")
        services.create_invitation(
            workspace=ws,
            email=other_user.email,
            role=Role.MEMBER,
            invited_by=user,
        )

        assert [t for t, _ in letters] == [services.NOTIFICATION_INVITATION]
        assert (
            _template_of(letters[0][0])
            == "notifications/email/workspace_invitation.html"
        )

    def test_the_match_is_case_insensitive_like_the_recipient_lookup(
        self, user, other_user, letters
    ):
        """An address is one address whatever its capitalization.

        The ``user_id`` targeting below already matched case-insensitively;
        a branch that did not would mail the create-your-account copy to a
        person who has an account and is simply shouting their email.
        """
        ws = services.create_workspace(user=user, name="Acme")
        services.create_invitation(
            workspace=ws,
            email=other_user.email.upper(),
            role=Role.MEMBER,
            invited_by=user,
        )

        assert [t for t, _ in letters] == [services.NOTIFICATION_INVITATION]

    def test_a_resend_is_the_reminder_whoever_the_invitee_is(
        self, user, letters
    ):
        """The explicit type still wins: a resend is a reminder, not a
        first invitation, and that is true for a stranger too."""
        ws = services.create_workspace(user=user, name="Acme")
        inv = services.create_invitation(
            workspace=ws,
            email="stranger@example.com",
            role=Role.MEMBER,
            invited_by=user,
        )
        inv.last_sent_at = timezone.now() - timezone.timedelta(days=1)
        inv.save(update_fields=["last_sent_at"])
        letters.clear()

        services.resend_invitation(invitation=inv)

        assert [t for t, _ in letters] == [services.NOTIFICATION_INVITATION_REMINDER]
        assert (
            _template_of(letters[0][0])
            == "notifications/email/workspace_invitation_reminder.html"
        )

    def test_the_two_letters_are_otherwise_identical_requests(
        self, user, other_user, letters
    ):
        """The branch changes the TYPE and nothing else.

        Same variables, same recipient address. If a future edit made the
        stranger's request carry different variables, the two letters would
        stop being the same letter in two voices — and the safety argument
        for branching on account existence at all (see
        ``_send_invitation_notification``) would need re-making.
        """
        ws = services.create_workspace(user=user, name="Acme")
        services.create_invitation(
            workspace=ws, email="stranger@example.com", role=Role.MEMBER,
            invited_by=user,
        )
        services.create_invitation(
            workspace=ws, email=other_user.email, role=Role.MEMBER,
            invited_by=user,
        )

        (_, stranger), (_, known) = letters
        assert set(stranger["variables"]) == set(known["variables"])
        assert stranger["variables"]["workspace_name"] == "Acme"
        assert stranger["email"] == "stranger@example.com"
        assert known["email"] == other_user.email.lower()
        # The one documented difference, and it predates the branch: a known
        # invitee also rides by user_id so notifications can apply that
        # account's language and channel preferences.
        assert "user_id" not in stranger
        assert known["user_id"] == str(other_user.pk)


# ---------------------------------------------------------------------------
# The class: no letter in the catalog may be unreachable from this package
# ---------------------------------------------------------------------------


def _package_trees() -> list[ast.Module]:
    return [
        ast.parse(path.read_text(encoding="utf-8"))
        for path in sorted(PACKAGE.glob("*.py"))
    ]


def _module_constants(trees) -> dict[str, str]:
    """``{NAME: "literal"}`` for the package's module-level string constants."""
    constants: dict[str, str] = {}
    for tree in trees:
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            if not isinstance(node.value, ast.Constant) or not isinstance(
                node.value.value, str
            ):
                continue
            for target in node.targets:
                if isinstance(target, ast.Name):
                    constants[target.id] = node.value.value
    return constants


def _strings(node, constants: dict[str, str]) -> set[str]:
    """Every string *node* can evaluate to: a literal, a named constant, or
    either arm of a conditional expression."""
    if isinstance(node, ast.Constant):
        return {node.value} if isinstance(node.value, str) else set()
    if isinstance(node, ast.Name):
        return {constants[node.id]} if node.id in constants else set()
    if isinstance(node, ast.IfExp):
        return _strings(node.body, constants) | _strings(node.orelse, constants)
    return set()


def _requested_notification_types() -> set[str]:
    """Notification types this package can actually request, from its AST.

    Static, not runtime, on purpose: a runtime probe finds only the types
    the tests happen to drive, which is the same blind spot that let
    ``.new_user`` sit unused for two minor versions. Two passes over the
    package source:

    1. every ``request_notification(<value>, ...)``, where *value* is a
       literal, a module constant, or a conditional between them — the
       branch in ``_send_invitation_notification`` is the third shape;
    2. every ``request_notification(<name>, ...)`` where *name* is a
       parameter of the enclosing function (the ``_send_*_notification``
       helpers): the parameter's default, anything the body assigns to it,
       and every value any call site in the package passes for it.

    A type spelled in a way neither pass can resolve simply does not appear
    here, which surfaces as "unreachable" — the gate fails loudly rather
    than passing quietly on a call it could not read, the correct direction
    for a rot detector.
    """
    trees = _package_trees()
    constants = _module_constants(trees)
    found: set[str] = set()
    #: {enclosing function name: parameter name that carries the type}
    threaded: dict[str, str] = {}

    for tree in trees:
        for func in ast.walk(tree):
            if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            args = func.args
            params = {a.arg for a in args.args + args.kwonlyargs}
            defaults = {
                a.arg: d
                for a, d in zip(args.kwonlyargs, args.kw_defaults)
                if d is not None
            }
            for call in ast.walk(func):
                if not isinstance(call, ast.Call):
                    continue
                if getattr(call.func, "id", None) != "request_notification":
                    continue
                if not call.args:
                    continue
                first = call.args[0]
                found |= _strings(first, constants)
                if isinstance(first, ast.Name) and first.id in params:
                    threaded[func.name] = first.id
                    if first.id in defaults:
                        found |= _strings(defaults[first.id], constants)
                    # Whatever the body decides the parameter is.
                    for node in ast.walk(func):
                        if isinstance(node, ast.Assign) and any(
                            isinstance(t, ast.Name) and t.id == first.id
                            for t in node.targets
                        ):
                            found |= _strings(node.value, constants)

    # Pass 2: what every call site hands those helpers.
    for tree in trees:
        for call in ast.walk(tree):
            if not isinstance(call, ast.Call):
                continue
            name = getattr(call.func, "id", None) or getattr(call.func, "attr", None)
            param = threaded.get(name)
            if param is None:
                continue
            values = [kw.value for kw in call.keywords if kw.arg == param]
            values += list(call.args)
            for value in values:
                found |= _strings(value, constants)
    return found


class TestEveryWorkspaceLetterIsReachable:
    """The catalog and the code must name the same set of letters."""

    @property
    def catalog(self) -> set[str]:
        return {
            t
            for t in notifications.NOTIFICATION_ROUTING
            if t.startswith(PREFIX)
        }

    def test_the_scanner_finds_the_types_it_is_supposed_to_find(self):
        """Guard the gate itself: a scanner that silently found nothing
        would make every assertion below vacuously... loud, actually — but
        this pins the resolution of BOTH shapes (a literal at the call
        site, and a value threaded through a parameter)."""
        found = _requested_notification_types()
        assert services.NOTIFICATION_INVITATION_NEW_USER in found  # via parameter
        assert "workspace.provisioned_account" in found  # literal at the call

    def test_no_letter_in_the_catalog_is_unreachable(self):
        """The ``.new_user`` defect, generalized to every workspace letter.

        A type with a template, a routing entry and translations that no
        code path selects is work that shipped and never ran. Add the
        branch that selects it, or delete it from the catalog — but it must
        not sit there looking finished.
        """
        unreachable = self.catalog - _requested_notification_types()
        assert not unreachable, (
            "stapel-notifications ships these workspace letters and nothing "
            f"in this package ever requests them: {sorted(unreachable)}"
        )

    def test_no_letter_this_module_sends_is_missing_from_the_catalog(self):
        """The mirror image, and just as silent.

        ``request_notification`` with a type the catalog does not know logs
        an error and drops the request — the caller sees success and the
        recipient sees nothing.
        """
        requested = {
            t for t in _requested_notification_types() if t.startswith(PREFIX)
        }
        missing = requested - self.catalog
        assert not missing, (
            "this package requests notification types the installed "
            f"stapel-notifications catalog does not carry: {sorted(missing)}"
        )
