"""URL configuration used only during tests.

Mounts the workspaces API at /workspaces/api/workspaces to match the test fixtures.
Tests call paths without a trailing slash on the collection (/workspaces/api/workspaces)
but with a slash before resource IDs (/workspaces/api/workspaces/{uuid}).
Two separate includes cover both cases:
  - path with '/' strips 'workspaces/api/workspaces/' → passes bare '{uuid}' to sub-patterns
  - path without '/' strips 'workspaces/api/workspaces' → passes '' to sub-patterns (list)
"""

from django.urls import include, path

urlpatterns = [
    # Detail/invite routes: URL has slash before UUID, so this prefix strips correctly.
    path("workspaces/api/workspaces/", include("stapel_workspaces.urls_v1")),
    # List/create route: URL has no trailing slash; this prefix matches and passes '' to sub-patterns.
    path("workspaces/api/workspaces", include("stapel_workspaces.urls_v1")),
]
