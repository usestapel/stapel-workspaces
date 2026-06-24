from django.contrib import admin

from .models import Workspace, WorkspaceInvitation, WorkspaceMember


@admin.register(Workspace)
class WorkspaceAdmin(admin.ModelAdmin):
    list_display = ["name", "slug", "type", "owner", "storage_used_bytes", "created_at"]
    list_filter = ["type", "created_at"]
    search_fields = ["name", "slug", "owner__email"]
    readonly_fields = ["id", "created_at", "updated_at"]


@admin.register(WorkspaceMember)
class WorkspaceMemberAdmin(admin.ModelAdmin):
    list_display = ["workspace", "user", "role", "invited_at", "accepted_at"]
    list_filter = ["role"]
    search_fields = ["workspace__name", "user__email"]
    readonly_fields = ["id", "invited_at"]


@admin.register(WorkspaceInvitation)
class WorkspaceInvitationAdmin(admin.ModelAdmin):
    list_display = ["workspace", "email", "role", "expires_at", "accepted_at", "revoked_at"]
    list_filter = ["role", "accepted_at", "revoked_at"]
    search_fields = ["workspace__name", "email"]
    readonly_fields = ["id", "token", "created_at"]
