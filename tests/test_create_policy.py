"""WHO MAY FOUND A WORKSPACE — the axis a private cloud was missing.

Until this key, ``POST /workspaces`` asked one question: are you a real
account? On a public cloud that is the right answer. On a PRIVATE one it is
not: an instance where entry is by invitation only, and where the operator
provisions the space, still let every invited member mint their own
organization beside it — stepping outside the org they were invited into,
inside somebody else's deployment.

The policy is derived from ``STREET_LANDING_MODE`` unless stated, because the
two answer the same product question and a deployment that closed its landing
mode and left creation open would have that gap without noticing.
"""
import pytest
from django.core.management import call_command
from django.core.management.base import SystemCheckError
from django.test import override_settings

from stapel_workspaces.checks import check_workspace_create_policy
from stapel_workspaces.conf import workspace_create_policy
from stapel_workspaces.models import Role, WorkspaceMember
from stapel_workspaces.services import (
    can_create_workspace,
    create_workspace,
    instance_owner_ids,
)

LIST_URL = "/workspaces/api/workspaces/v1/"


def _settings(**overrides):
    """STAPEL_WORKSPACES with only the keys under test replaced.

    Written out rather than mutating a shared dict: `override_settings` swaps
    the whole namespace, and a test that dropped ROLES would silently change
    what every capability check answers.
    """
    from stapel_workspaces.conf import DEFAULTS

    return {**DEFAULTS, **overrides}


class TestDerivedPolicy:
    def test_a_personal_landing_instance_is_open(self):
        with override_settings(STAPEL_WORKSPACES=_settings(STREET_LANDING_MODE="personal")):
            assert workspace_create_policy() == "open"

    def test_a_closed_landing_instance_restricts_to_the_instance_owner(self):
        """The owner's rule, stated as code: "если у миттудей стоит режим
        приватного облака — тогда создавать новые воркспейсы может только
        овнер облака"."""
        with override_settings(STAPEL_WORKSPACES=_settings(STREET_LANDING_MODE="none")):
            assert workspace_create_policy() == "instance_owner"

    def test_an_explicit_value_wins_over_the_derivation(self):
        with override_settings(
            STAPEL_WORKSPACES=_settings(
                STREET_LANDING_MODE="none", WORKSPACE_CREATE_POLICY="open"
            )
        ):
            assert workspace_create_policy() == "open"

    def test_a_misspelling_resolves_restrictively_and_is_an_error(self):
        """Degrading a typo to "open" would hand every member of a private
        cloud their own org — the exact failure the key exists to prevent. The
        loud half is E010."""
        with override_settings(
            STAPEL_WORKSPACES=_settings(WORKSPACE_CREATE_POLICY="instance-owner")
        ):
            assert workspace_create_policy() == "instance_owner"
            ids = [e.id for e in check_workspace_create_policy(None)]
            assert ids == ["stapel_workspaces.E010"]


@pytest.mark.django_db
class TestGate:
    def test_open_lets_any_account_create(self, authed_client, user):
        with override_settings(STAPEL_WORKSPACES=_settings(WORKSPACE_CREATE_POLICY="open")):
            res = authed_client.post(LIST_URL, {"name": "Mine"}, format="json")
        assert res.status_code == 201, res.content

    def test_closed_refuses_everybody(self, authed_client, user):
        with override_settings(STAPEL_WORKSPACES=_settings(WORKSPACE_CREATE_POLICY="closed")):
            res = authed_client.post(LIST_URL, {"name": "Mine"}, format="json")
        assert res.status_code == 403, res.content
        assert res.json()["localizable_error"] == "error.403.workspace_creation_closed"

    def test_instance_owner_may_create(self, authed_client, user):
        """The instance owner is the OWNER of the instance's default
        workspace — no second authority to keep in sync with the first."""
        home = create_workspace(user=user, name="The Instance")
        with override_settings(
            STAPEL_WORKSPACES=_settings(
                WORKSPACE_CREATE_POLICY="instance_owner",
                DEFAULT_WORKSPACE_ID=str(home.id),
            )
        ):
            assert instance_owner_ids() == {user.pk}
            res = authed_client.post(LIST_URL, {"name": "Another"}, format="json")
        assert res.status_code == 201, res.content

    def test_a_mere_member_of_the_instance_workspace_may_not(
        self, authed_client, user, other_user
    ):
        """THE DEFECT. Being invited into the cloud is not permission to found
        an organization inside it."""
        home = create_workspace(user=other_user, name="The Instance")
        WorkspaceMember.objects.create(
            workspace=home, user=user, role=Role.ADMIN, accepted_at="2026-01-01T00:00:00Z"
        )
        with override_settings(
            STAPEL_WORKSPACES=_settings(
                WORKSPACE_CREATE_POLICY="instance_owner",
                DEFAULT_WORKSPACE_ID=str(home.id),
            )
        ):
            assert not can_create_workspace(user)
            res = authed_client.post(LIST_URL, {"name": "Mine"}, format="json")
        assert res.status_code == 403, res.content

    def test_no_default_workspace_means_nobody(self, authed_client, user):
        """An unfinished private deployment refuses everyone — and says so at
        boot rather than at the first 403."""
        create_workspace(user=user, name="Some space")
        with override_settings(
            STAPEL_WORKSPACES=_settings(
                WORKSPACE_CREATE_POLICY="instance_owner", DEFAULT_WORKSPACE_ID=""
            )
        ):
            assert instance_owner_ids() == set()
            assert not can_create_workspace(user)
            ids = [w.id for w in check_workspace_create_policy(None)]
        assert ids == ["stapel_workspaces.W002"]


@pytest.mark.django_db
class TestListFlag:
    def test_the_list_answers_for_this_caller(self, authed_client, user):
        """The ANSWER rides the list, not the policy name: a client that had to
        resolve "instance_owner" itself would need the instance-owner lookup,
        and a client that got it wrong draws a button that 403s."""
        with override_settings(STAPEL_WORKSPACES=_settings(WORKSPACE_CREATE_POLICY="open")):
            assert authed_client.get(LIST_URL).json()["can_create_workspace"] is True
        with override_settings(STAPEL_WORKSPACES=_settings(WORKSPACE_CREATE_POLICY="closed")):
            assert authed_client.get(LIST_URL).json()["can_create_workspace"] is False

    def test_the_flag_and_the_door_agree(self, authed_client, user, other_user):
        """One helper answers both, so a drawn button always opens."""
        home = create_workspace(user=other_user, name="The Instance")
        with override_settings(
            STAPEL_WORKSPACES=_settings(
                WORKSPACE_CREATE_POLICY="instance_owner",
                DEFAULT_WORKSPACE_ID=str(home.id),
            )
        ):
            flag = authed_client.get(LIST_URL).json()["can_create_workspace"]
            created = authed_client.post(LIST_URL, {"name": "X"}, format="json")
        assert flag is False
        assert created.status_code == 403


def test_the_system_check_is_registered():
    """A check nobody runs is a comment. `manage.py check` must REACH it.

    Asserts the id appears in the failure text, not merely that `check` failed:
    this test harness carries several unrelated pre-existing errors (admin
    middleware, the double-mount urlconf), so "it raised" would have passed
    with this check deleted.
    """
    with override_settings(
        STAPEL_WORKSPACES=_settings(WORKSPACE_CREATE_POLICY="bogus")
    ):
        with pytest.raises(SystemCheckError) as raised:
            call_command("check")
    assert "stapel_workspaces.E010" in str(raised.value)
