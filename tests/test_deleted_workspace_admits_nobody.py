"""A soft-deleted workspace answers "not a member" on every seam.

``has_active_mandate`` filtered ``workspace.deleted_at``; ``get_membership``
did not, so after a delete the comm provider still told peers
``is_member: true`` and they kept granting access.
"""
import pytest
from django.utils import timezone

from stapel_core.comm import call

from stapel_workspaces import services
from stapel_workspaces.permissions import get_membership, has_capability


@pytest.mark.django_db
def test_deleted_workspace_admits_nobody(user):
    ws = services.create_workspace(user=user, name="Acme")
    assert get_membership(ws.id, user.pk) is not None

    ws.deleted_at = timezone.now()
    ws.save(update_fields=["deleted_at"])

    assert get_membership(ws.id, user.pk) is None
    assert get_membership(ws.id, user.pk, include_suspended=True) is None
    assert not has_capability(ws.id, user.pk, "workspace.view")
    answer = call(
        "workspaces.check_membership",
        {"workspace_id": str(ws.id), "user_id": str(user.pk)},
    )
    assert answer == {"is_member": False, "role": None, "capabilities": []}
