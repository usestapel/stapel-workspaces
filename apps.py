from django.apps import AppConfig


class WorkspacesConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "stapel_workspaces"
    label = 'workspaces'
    verbose_name = "Stapel Workspaces"

    def ready(self):
        from stapel_core.gdpr import gdpr_registry
        from .gdpr import WorkspacesGDPRProvider
        gdpr_registry.register(WorkspacesGDPRProvider())

        # Action subscriptions (in-process in a monolith, bus consumer in
        # microservices — same code, transport chosen by STAPEL_COMM).
        from . import actions  # noqa: F401

        # Function providers (workspaces.check_membership). register() is
        # idempotent — ready() may run more than once.
        from . import functions
        functions.register()
