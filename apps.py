from django.apps import AppConfig


class WorkspacesConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "stapel_workspaces"
    label = 'workspaces'
    verbose_name = "Iron Workspaces"

    def ready(self):
        from stapel_core.gdpr import gdpr_registry
        from .gdpr import WorkspacesGDPRProvider
        gdpr_registry.register(WorkspacesGDPRProvider())
