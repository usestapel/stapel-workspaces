"""Single-module Django settings for stapel-workspaces's harnesses.

Single source of truth for the ``settings.configure(...)`` block shared by:

  - the pytest suite (``conftest.py``) — mounts workspaces on its test-only
    urlconf (``stapel_workspaces.conftest_urls``); and
  - the contract-emission harness (``_codegen.py`` / ``make contract``) — mounts
    workspaces on its *canonical* public API prefix
    (``stapel_workspaces.codegen_urls`` → ``workspaces/api/``) and enables
    drf-spectacular, so the emitted ``schema.json`` / ``flows.json`` paths are
    byte-identical to the monolith aggregate's workspaces slice
    (contract-pipeline.md §2).

Keeping one copy here means the harness and the tests can never drift in their
``INSTALLED_APPS`` / mock config — the exact hazard contract-pipeline.md §3
calls out ("~30 lines that *reference* the already-existing config, not a
second copy of it"). Mirrors stapel-auth's ``_codegen_settings.py`` (the
per-module contract pipeline etalon).
"""
from __future__ import annotations


#: Env flag that co-installs stapel-profiles into the TEST harness.
#:
#: Off by default, and that default is load-bearing. stapel-profiles is not
#: a dependency of this distribution and never will be (MODULE.md: modules
#: do not import each other) — but the roster's two name-edit endpoints
#: write the display name that module owns, through the in-process seam in
#: ``services``, and no fake proves that a real ``Profile`` row takes the
#: write. So the suite can be run a second time with the sibling actually
#: mounted, the way a monolith deploys, and the handful of tests that need
#: a real profile row run only then (they skip otherwise).
#:
#: Why not simply always: installing the sibling registers ITS error keys
#: into the process-global service-error registry, which is what this
#: module's i18n catalog gate and error-key tests read. A default-on
#: co-mount would pull ~50 foreign keys into ``translations/errors.ru.json``
#: — corrupting this module's own contract artifacts to test one seam.
#: Two sessions keep both honest. CI runs both.
PROFILES_TEST_APP_ENV = "STAPEL_WORKSPACES_TEST_PROFILES"


def profiles_co_mounted() -> bool:
    """Should the test harness mount stapel-profiles alongside this module?

    True only when :data:`PROFILES_TEST_APP_ENV` is set AND the package is
    actually importable, so a bare checkout with the flag set degrades to a
    plain (skipping) run instead of an INSTALLED_APPS import error.
    """
    import os
    from importlib.util import find_spec

    if not os.environ.get(PROFILES_TEST_APP_ENV):
        return False
    try:
        return find_spec("stapel_profiles") is not None
    except (ImportError, ValueError):  # pragma: no cover - broken install
        return False


def settings_kwargs(
    *,
    root_urlconf: str = "stapel_workspaces.conftest_urls",
    contract: bool = False,
) -> dict:
    """Return the ``settings.configure(**kwargs)`` for a single-module workspaces
    instance.

    ``root_urlconf`` selects the mount: the test-only double-mount
    (``stapel_workspaces.conftest_urls``) for the test suite, canonical-prefix
    (``stapel_workspaces.codegen_urls`` → ``workspaces/api/``) for contract
    emission.

    ``contract=True`` swaps in the *production* ``REST_FRAMEWORK`` (the canonical
    stapel-core config, inlined as plain dotted paths — importing it would trip
    the same chicken-and-egg as spectacular). This matters for byte-identity: the
    monolith emits with ``DEFAULT_SCHEMA_CLASS=PermissionAwareAutoSchema`` and the
    real permission/renderer classes, and DRF caches ``REST_FRAMEWORK`` on first
    access, so it must be right at ``configure()`` time — a post-hoc assignment is
    too late. The test suite keeps its historical config (``contract=False``,
    no ``REST_FRAMEWORK`` key at all — every view here declares its own
    ``permission_classes``, so the DRF default was never exercised).

    ``SPECTACULAR_SETTINGS`` is deliberately *not* set. drf-spectacular builds its
    settings singleton at *import* time (``getattr(settings, 'SPECTACULAR_SETTINGS',
    {})`` at module load), before a ``configure()``-based harness can populate it,
    so a Django-level ``SPECTACULAR_SETTINGS`` is silently ignored and the emitter
    runs on drf **defaults**. That is exactly what the monolith aggregate emits
    with too. The one knob that still must be forced, ``SCHEMA_PATH_PREFIX``, is
    patched on the singleton directly by the harness (see ``_codegen._configure``).
    """
    kwargs = dict(
        SECRET_KEY="test-secret-key-not-for-production",
        INSTALLED_APPS=[
            "django.contrib.contenttypes",
            "django.contrib.auth",
            "django.contrib.sessions",
            "django.contrib.messages",
            "django.contrib.admin",
            # CommonDjangoConfig ships the stapel_core management commands
            # (generate_error_keys, used by the errors.json drift gate).
            "stapel_core.django.apps.CommonDjangoConfig",
            "stapel_core.django.users",
            # The membership journal's storage (audit.py writes the
            # workspace.audit stream; the default backend is this app's
            # table). Models only — it mounts no URLs, so the emitted
            # contract is unchanged by its presence.
            "stapel_core.django.eventstore",
            "rest_framework",
            "drf_spectacular",
            "stapel_workspaces",
        ],
        AUTH_USER_MODEL="users.User",
        DATABASES={
            "default": {
                "ENGINE": "django.db.backends.sqlite3",
                "NAME": ":memory:",
            }
        },
        DEFAULT_AUTO_FIELD="django.db.models.BigAutoField",
        USE_TZ=True,
        APPEND_SLASH=False,
        ROOT_URLCONF=root_urlconf,
        CACHES={
            "default": {
                "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            }
        },
        # In-memory bus — no Kafka/Redis broker needed
        STAPEL_BUS_BACKEND="stapel_core.bus.backends.memory.MemoryBus",
        # Synchronous in-process comm: no outbox table needed, emitted
        # actions reach in-process subscribers immediately.
        STAPEL_COMM={
            "ACTION_TRANSPORT": "inprocess",
            "FUNCTION_TRANSPORT": "inprocess",
            "OUTBOX_ENABLED": False,
            "VALIDATE_SCHEMAS": True,
        },
        SERVICE_NAME="workspaces",
        FRONTEND_URL="https://app.example.com",
        # Skip migrations — create tables directly from models
        MIGRATION_MODULES={
            "users": None,
            "workspaces": None,
            "stapel_eventstore": None,
        },
    )
    if not contract and profiles_co_mounted():
        # Opt-in, test harness only, and deliberately never the contract
        # harness: the emitted triad must stay the byte-identical
        # {workspaces + core} slice of the monolith aggregate
        # (contract-pipeline.md §2), and a co-mounted sibling would drag its
        # paths and components into it.
        kwargs["INSTALLED_APPS"] = [*kwargs["INSTALLED_APPS"], "stapel_profiles"]
        kwargs["MIGRATION_MODULES"]["profiles"] = None
    if contract:
        # Mirror stapel_core.django.settings.REST_FRAMEWORK exactly (the config
        # the monolith emits under). Inlined, not imported, to dodge the
        # import-time settings read; kept in lockstep by test_contract.py's
        # identity gate.
        kwargs["REST_FRAMEWORK"] = {
            "DEFAULT_AUTHENTICATION_CLASSES": [
                "stapel_core.django.jwt.authentication.JWTCookieAuthentication",
            ],
            "DEFAULT_PERMISSION_CLASSES": [
                "stapel_core.django.api.permissions.IsServiceRequest",
                "stapel_core.django.api.permissions.IsSuperUser",
            ],
            "DEFAULT_RENDERER_CLASSES": [
                "rest_framework.renderers.JSONRenderer",
                "rest_framework.renderers.BrowsableAPIRenderer",
            ],
            "DEFAULT_SCHEMA_CLASS": "stapel_core.django.openapi.schemas.PermissionAwareAutoSchema",
            "EXCEPTION_HANDLER": "stapel_core.django.api.errors.stapel_exception_handler",
        }
    return kwargs


# The multi-module common path prefix drf-spectacular auto-detects in the monolith
# aggregate. Forced on the drf-spectacular settings singleton by the harness so a
# single-module instance derives the same operationIds (see _codegen._configure and
# the SCHEMA_PATH_PREFIX note above). Uniform across all five pair-backends.
CODEGEN_SCHEMA_PATH_PREFIX = "/"
