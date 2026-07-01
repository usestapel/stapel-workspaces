"""URL configuration used only during tests.

Mounts the workspaces API at /workspaces/api/workspaces/ to match the test fixtures.
"""

from django.urls import include, path

urlpatterns = [
    path("workspaces/api/workspaces", include("stapel_workspaces.urls")),
]
