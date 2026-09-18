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

        # System checks: the STAPEL_WORKSPACES role/capability registries
        # drive authorization — a malformed overlay must block deploys.
        from . import checks  # noqa: F401

        # Action subscriptions (in-process in a monolith, bus consumer in
        # microservices — same code, transport chosen by STAPEL_COMM).
        from . import actions  # noqa: F401

        # Function providers (workspaces.check_membership). register() is
        # idempotent — ready() may run more than once.
        from . import functions
        functions.register()

        # The erasure protocol, implemented once in stapel-core:
        # gdpr.erasure.requested -> erase -> gdpr.section.erased with a
        # deterministic receipt inside the erase's transaction, the
        # gdpr.owner.probe answer from the same subscriber, and the
        # deprecated user.deleted account path. No protocol code here.
        from stapel_core.gdpr import register_gdpr_owner

        from .erasure import GDPR_OWNER, GDPR_SUBJECT_TYPES, erase_subject

        register_gdpr_owner(GDPR_OWNER, GDPR_SUBJECT_TYPES, erase_subject)
