"""Canonical-prefix URLconf for contract emission (contract-pipeline.md §2).

The pytest urlconf (``conftest_urls.py``) mounts workspaces under a test-only
double include to satisfy fixture path conventions (with/without a trailing
slash before the collection). That is not the repoint's mount: the monolith
aggregate — and therefore every frontend projection — serves workspaces under
its canonical public API prefix, ``/workspaces/api/...``
(stapel-example-monolith/svc-app/core/urls.py: ``path("workspaces/api/",
include("stapel_workspaces.urls"))``). No sibling module is co-mounted under
this prefix in the monolith (unlike auth+gdpr), so this urlconf mounts
workspaces alone.

This URLconf reproduces the monolith mount exactly, so drf-spectacular emits
``/workspaces/api/...`` paths (and the matching ``workspaces_api_*``
operationIds) and ``generate_flow_docs`` resolves flow endpoints to the same.
Getting this prefix exact is the make-or-break for a zero-diff repoint
(contract-pipeline.md §2, §9).
"""
from django.conf.urls import include
from django.urls import path

urlpatterns = [
    path("workspaces/api/", include("stapel_workspaces.urls")),
]
