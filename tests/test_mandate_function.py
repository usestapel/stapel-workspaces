"""``workspaces.check_mandate`` — the answering half of core's mandate seam.

``has_active_mandate`` existed and had zero consumers outside this package,
because a sibling service that does not embed this app could not reach it:
the comm surface published only workspace-scoped questions
(``check_membership`` / ``check_capability``), and neither answers "does this
user hold a mandate ANYWHERE". This provider is that answer.

What is pinned: the wire form and the in-process predicate never disagree,
including on the three cases that make a membership row worthless (pending,
suspended, workspace deleted).
"""
import pytest

from stapel_core.comm import call, function_registry
from stapel_core.django.mandate import (
    MANDATE_FUNCTION,
    MANDATE_RESULT_KEY,
    MandateState,
    mandate_state,
)
from stapel_workspaces.functions import CHECK_MANDATE, check_mandate, register
from stapel_workspaces.models import Role, WorkspaceMember
from stapel_workspaces.permissions import has_active_mandate
from stapel_workspaces.services import create_workspace


def test_the_name_is_cores_contract():
    """A provider registered under a name core does not ask for is unreachable
    in exactly the way that looks like it works."""
    assert CHECK_MANDATE == MANDATE_FUNCTION


@pytest.mark.django_db
class TestTheAnswer:
    def _ask(self, user):
        return check_mandate({"user_id": str(user.pk)})[MANDATE_RESULT_KEY]

    def test_a_member_holds_a_mandate(self, user):
        create_workspace(user=user, name="Acme")
        assert self._ask(user) is True

    def test_a_mandate_less_account_does_not(self, user):
        assert self._ask(user) is False

    def test_a_pending_invitation_is_not_a_mandate(self, user, other_user):
        ws = create_workspace(user=user, name="Acme")
        WorkspaceMember.objects.create(workspace=ws, user=other_user, role=Role.MEMBER)
        assert self._ask(other_user) is False

    def test_the_wire_form_never_disagrees_with_the_predicate(self, user, other_user):
        """One predicate, two front doors. Two copies of the `active()` filter
        is how the wire answer and the local one start drifting."""
        for subject in (user, other_user):
            assert self._ask(subject) is has_active_mandate(subject)
        create_workspace(user=user, name="Acme")
        for subject in (user, other_user):
            assert self._ask(subject) is has_active_mandate(subject)


@pytest.mark.django_db
class TestThroughTheSeam:
    """Registered, called by name, consumed by core — the whole path."""

    @pytest.fixture(autouse=True)
    def registered(self):
        register()
        yield
        function_registry._providers.pop(CHECK_MANDATE, None)

    def test_core_reads_a_guest_as_guest(self, user):
        assert mandate_state(user) is MandateState.GUEST

    def test_core_reads_a_member_as_mandated(self, user):
        create_workspace(user=user, name="Acme")
        assert mandate_state(user) is MandateState.MANDATED

    def test_the_payload_matches_the_declared_schema(self, user):
        result = call(MANDATE_FUNCTION, {"user_id": str(user.pk)})
        assert result == {MANDATE_RESULT_KEY: False}
