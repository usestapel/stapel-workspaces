"""E2E host settings — a real minimal host mounting auth + workspaces.

Not shipped in the wheel (the setuptools packages list is explicit). Used by
``e2e/run_e2e.py`` to prove the workspace deletion lifecycle over real HTTP,
against a real database and the real event store — including the refusals,
which are the half a unit test can assert but a deployment can still get
wrong (a 409 that never reaches the client as a keyed code is a 409 nobody
can render).

``DEFAULT_WORKSPACE_ID`` is read from the environment because that is how a
deployment actually declares it: the runner creates the instance's home
workspace BEFORE booting this host and passes its id in, exactly as an
operator would put it in config.
"""
import os
import tempfile
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

SECRET_KEY = "e2e-only-not-a-secret"
DEBUG = True
ALLOWED_HOSTS = ["*"]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "drf_spectacular",
    "stapel_core.django.apps.CommonDjangoConfig",
    "stapel_core.django.users",
    "stapel_core.django.outbox",
    # The audit journal lives in the core event store, not a table of the
    # roster's own — so the store is part of a host that reads it.
    "stapel_core.django.eventstore",
    "stapel_auth",
    "stapel_workspaces",
]

AUTH_USER_MODEL = "users.User"

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "e2e.urls"

from stapel_core.django.settings import get_common_templates  # noqa: E402

TEMPLATES = get_common_templates(BASE_DIR)

_STATE_DIR = Path(
    os.environ.get(
        "STAPEL_WORKSPACES_E2E_DIR", tempfile.gettempdir() + "/stapel-workspaces-e2e"
    )
)
_STATE_DIR.mkdir(parents=True, exist_ok=True)

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": _STATE_DIR / "db.sqlite3",
    }
}

STATIC_URL = "/static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
USE_TZ = True

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "stapel_core.django.jwt.authentication.JWTCookieAuthentication",
    ],
    "EXCEPTION_HANDLER": "stapel_core.django.api.errors.stapel_exception_handler",
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
}
SPECTACULAR_SETTINGS = {"TITLE": "stapel-workspaces E2E", "VERSION": "0.1.0"}

STAPEL_COMM = {
    "OUTBOX_ENABLED": True,
    "ACTION_TRANSPORT": "inprocess",
}

STAPEL_AUTH = {
    "AUTH_PASSWORD_LOGIN": True,
    "AUTH_EMAIL_LOGIN": False,
    "AUTH_OAUTH_LOGIN": False,
    "AUTH_EMAIL_REGISTRATION": False,
    "AUTH_OAUTH_REGISTRATION": False,
}

STAPEL_WORKSPACES = {
    # Declared by the operator, not discovered by the code — see the module
    # docstring. Empty on the first boot, set on the second.
    "DEFAULT_WORKSPACE_ID": os.environ.get("STAPEL_WORKSPACES_E2E_DEFAULT_WS", ""),
    # The instance re-mints a personal workspace on landing, which is what
    # makes deleting one a refusal rather than a deletion.
    "STREET_LANDING_MODE": "personal",
    # Anyone may found a workspace here: the run creates several, and the
    # instance-owner policy would make that a test of the create gate
    # instead of the delete gate.
    "WORKSPACE_CREATE_POLICY": "open",
    # This host sells nothing, so it says so rather than leaving plan
    # ceilings to fail closed against a billing service that is not here
    # (checks.E011). The declaration is the supported answer, not a bypass.
    "ALLOW_UNBILLED": True,
}

# No broker in this host: background work runs inline so a login that
# queues an audit task does not fail on a connection refused.
CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True

STAPEL_SERVICES = [{"name": "stapel-workspaces E2E", "prefix": ""}]
EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
