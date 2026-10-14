"""Every response body the contract declares is a body the views actually send.

``docs/schema.json`` is emitted from the views' ``@extend_schema``
annotations, and an annotation is a CLAIM: it says what the view returns, and
the generator has no way to check it against the method body.
``tests/test_contract.py`` compares the committed document against a FRESH
EMISSION of the same annotations — it proves the file is not stale, and
nothing else, because both sides come from the claim. stapel-alerts 0.2.0
shipped ``GET /issues`` declared as ``Issue[]`` while the wire carried
``{count, offset, limit, results}``: the drift gate was green and the
frontend pair rendered ``undefined``.

This is the gate the generator cannot be: it performs every operation the
committed schema declares with a JSON response body, and validates the body
it gets against the schema it was promised.

Rules this file holds itself to:

* an operation with a declared JSON response and no entry in ``RECIPES``
  FAILS LOUDLY — a gate that quietly covers three of four rows is the family
  of green that proves nothing;
* a path parameter the gate cannot fill fails at the point of substitution,
  naming the operation;
* an operation that genuinely cannot run in-process is listed by name in
  ``UNDRIVABLE`` with a one-line reason. That list is asserted to be exactly
  current: a stale entry, or a missing reason, fails;
* a collection that comes back empty fails in the populated pass — an empty
  array validates against any item schema, so an empty answer is a check that
  looked at nothing;
* every read, and every write with a nullable answer, is driven a SECOND time
  in its emptiest legal state (``EMPTY_STATE``). Every null finding in the
  first wave of this gate was there: an ``exp`` that is null for every active
  token, a counter null for every account without the feature, a
  ``created_at`` null for every account that had just signed up.

Runs on every interpreter: it reads the committed schema and never emits.

THE MOUNT. ``codegen_urls.py`` mounts ``workspaces/api/`` and the module's
own ``urls.py`` contributes ``v1/``, so the document is written against
``/workspaces/api/v1/…``. The pytest urlconf (``conftest_urls.py``) mounts
``workspaces/api/workspaces/`` — a doubled segment that reproduces one host's
deployed prefix — so **not one path in the committed contract resolves under
the suite's own urlconf**, and nothing in this repository had ever driven the
document it ships. That is the same defect three of the first four libraries
in this wave carried, and ``test_every_declared_path_resolves_under_this_urlconf``
below is what catches it: this module declares the EMISSION mount and asserts
every declared path resolves under it.

What it found on its first run: 24 of 24 operations driven, 24 declared
rows, 0 red. The claims this module makes about its own wire are honest,
including every ``nullable`` one, and ``test_the_gate_is_not_blind`` proves
that is a finding rather than a gate that never looked: it re-validates every
driven body against ``{"type": "string"}`` and requires all of them to fail.

Two things this pass deliberately does not call a lie:

* ``GET /workspaces/api/v1/{workspace_id}/members`` and its two siblings
  annotate ``responses={200: XSerializer(many=True)}`` while the wire sends
  the anchor-pagination envelope ``{items, next_anchor, prev_anchor,
  has_next, has_prev, count}``. That is the stapel-alerts defect in shape —
  but NOT here: each of those views also declares ``pagination_class``, which
  drf-spectacular reads, and the committed document therefore already says
  ``Paginated…List``. The annotation and the document disagree; the document
  and the wire do not, and the document is what clients are generated from.
* ``POST /workspaces/api/v1/{workspace_id}/members/provision`` and
  ``…/password/reset`` both answer through a comm Function owned by
  stapel-auth. The transport is stood in for here (that is what a Function
  seam is for); everything on this side of it — the DTO, the serializer, the
  status — runs for real.
"""
import copy
import json
import re
import uuid
from pathlib import Path

import jsonschema
import pytest
from django.test import override_settings
from django.urls import include, path as url_path
from rest_framework.test import APIClient

REPO = Path(__file__).resolve().parent.parent
SCHEMA = json.loads((REPO / "docs" / "schema.json").read_text())

#: The mount the contract is emitted at, reproduced for the test client
#: (``codegen_urls.py``). The suite's own ``conftest_urls.py`` mounts a
#: doubled ``workspaces/api/workspaces/`` prefix under which NONE of these
#: paths resolve — see the module docstring.
urlpatterns = [
    url_path("workspaces/api/", include("stapel_workspaces.urls")),
]

pytestmark = [pytest.mark.django_db, pytest.mark.urls(__name__)]

V1 = "/workspaces/api/v1"


@pytest.fixture(autouse=True)
def _clocks_and_buckets():
    """Two rate limiters stood down, and neither is part of the contract.

    ``INVITATION_THROTTLE`` shares one anonymous per-IP bucket in the
    process-wide locmem cache with every other module in the run, so the
    public invite endpoints answer 429 depending on what ran before them.
    ``INVITATION_RESEND_COOLDOWN_SECONDS`` is a clock: a resend inside the
    window answers 429 by design, which says nothing about the 200 body the
    contract declares. Both are behaviour this module's own suites already
    pin; neither is a response shape.
    """
    with override_settings(
        STAPEL_WORKSPACES={
            "INVITATION_THROTTLE": None,
            "INVITATION_RESEND_COOLDOWN_SECONDS": 0,
        }
    ):
        yield


@pytest.fixture(autouse=True)
def _media_root(tmp_path):
    """Nothing here writes files today; pin the root so nothing ever does.

    ``MEDIA_ROOT`` is unset in this module's harness settings, so it defaults
    to the working directory — a single future upload would land in the
    checkout.
    """
    with override_settings(MEDIA_ROOT=str(tmp_path)):
        yield


# ─────────────────────────────────────────────────────────────────────────────
# The contract side: what the document declares
# ─────────────────────────────────────────────────────────────────────────────


def _json_schema(node):
    """OpenAPI 3.0 → JSON Schema, for the divergences that matter here.

    OAS 3.0 spells "may be null" as ``nullable: true`` beside a ``type`` (or
    beside an ``allOf`` wrapping a ``$ref``, which is how ``mfa_enforcement``
    is emitted); JSON Schema has no such keyword and would refuse the null —
    which is exactly the value most of these fields answer in their empty
    state. Everything else drf-spectacular emits here (``$ref``, ``allOf``,
    ``enum``, ``required``, ``additionalProperties``) is JSON Schema as
    written.
    """
    if isinstance(node, list):
        return [_json_schema(item) for item in node]
    if not isinstance(node, dict):
        return node
    rebuilt = {k: _json_schema(v) for k, v in node.items() if k != "nullable"}
    if node.get("nullable"):
        return {"anyOf": [rebuilt, {"type": "null"}]}
    return rebuilt


def _validator(response_schema):
    root = copy.deepcopy(response_schema)
    root["components"] = copy.deepcopy(SCHEMA["components"])
    return jsonschema.Draft202012Validator(_json_schema(root))


def _operations():
    """Every ``(method, path, 2xx code, JSON body schema)`` the contract declares."""
    ops = []
    for path, methods in SCHEMA["paths"].items():
        for method, op in methods.items():
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            for code, response in op.get("responses", {}).items():
                body = (
                    response.get("content", {})
                    .get("application/json", {})
                    .get("schema")
                )
                if body is not None and code.startswith("2"):
                    ops.append((method.upper(), path, int(code), body))
    return sorted(ops, key=lambda o: (o[1], o[0], o[2]))


OPERATIONS = _operations()


# ─────────────────────────────────────────────────────────────────────────────
# The wire side: harness
# ─────────────────────────────────────────────────────────────────────────────


def _unique(prefix):
    return f"{prefix}{uuid.uuid4().hex[:10]}"


def anonymous():
    return APIClient()


def make_user(**kwargs):
    from stapel_core.django.users.models import User

    defaults = dict(
        username=_unique("wire-"),
        email=f"{_unique('wire-')}@example.com",
        password="wire-contract-password-7",
    )
    defaults.update(kwargs)
    return User.objects.create_user(**defaults)


def client_for(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def staff_client():
    """``IsServiceRequest | IsStaffUser`` — the human half of the internal door.

    Deployment staff rather than an ``X-API-KEY`` header: the key path needs
    ``ServiceAPIKeyMiddleware`` swapped into MIDDLEWARE, and the permission
    is an OR, so the same view code answers either way.
    """
    return client_for(make_user(is_staff=True))


def make_workspace(user=None, name="Wire Contract Org"):
    from stapel_workspaces import services

    return services.create_workspace(user=user or make_user(), name=name)


def make_member(workspace, user=None, role=None, accepted=True, **kwargs):
    from django.utils import timezone

    from stapel_workspaces.models import Role, WorkspaceMember

    return WorkspaceMember.objects.create(
        workspace=workspace,
        user=user or make_user(),
        role=role or Role.MEMBER,
        accepted_at=timezone.now() if accepted else None,
        **kwargs,
    )


def make_invitation(workspace, inviter, email=None, role=None, display_name=""):
    from stapel_workspaces import services
    from stapel_workspaces.models import Role

    return services.create_invitation(
        workspace=workspace,
        email=email or f"{_unique('invitee-')}@example.com",
        role=role or Role.MEMBER,
        invited_by=inviter,
        display_name=display_name,
    )


def step_up(user):
    """A fresh HIGH step-up grant — what ``@requires_verification`` demands.

    The real ``stapel_core.verification`` grant store, scope ``sensitive``:
    the two strictest operations here are gated on a step-up, and skipping
    it would mean driving them through a door the deployment does not have.
    """
    from stapel_core.verification import grant_verification

    grant_verification(user_id=str(user.pk), scope="sensitive", max_age=300)


class function_seam:
    """Stand in for one comm Function another module owns, for one call.

    ``stapel-workspaces`` never imports its siblings; it calls them by name
    over the comm registry, and that name is the seam a deployment wires.
    Registering a scripted provider is how this module's own suites drive
    those paths, and it is the honest boundary: the payload contract is
    shared, the implementation is not this repository's.
    """

    def __init__(self, name, result):
        self.name = name
        self.result = result

    def __enter__(self):
        from stapel_core.comm.registry import function_registry

        # Displace the suite-wide stand-in where there is one: a function
        # name has exactly one provider.
        self._previous = function_registry._providers.pop(self.name, None)
        function_registry._schemas.pop(self.name, None)
        result = self.result
        function_registry.register(
            self.name, result if callable(result) else (lambda payload: result)
        )
        return self

    def __exit__(self, *exc):
        from stapel_core.comm.registry import function_registry

        function_registry._providers.pop(self.name, None)
        function_registry._schemas.pop(self.name, None)
        if self._previous is not None:
            function_registry._providers[self.name] = self._previous
        return False


def auth_provision(password=None):
    """``auth.provision_user`` — mints the account the membership points at."""
    from stapel_core.django.users.models import User

    from stapel_workspaces import services

    def provider(payload):
        created = User.objects.create_user(
            username=payload["username"],
            email=payload.get("email") or f"{_unique('provisioned-')}@example.com",
            password=payload.get("password") or "wire-generated-secret-1",
        )
        result = {"user_id": str(created.pk)}
        if not payload.get("password"):
            result["generated_password"] = "wire-generated-secret-1"
        return result

    return function_seam(services.PROVISION_USER, provider)


def auth_password_reset():
    """``auth.admin_reset_password`` — auth's half of the #110 seam."""
    from stapel_workspaces import services

    def provider(payload):
        result = {
            "sessions_revoked": 2,
            "first_login_policies_applied": list(
                payload.get("first_login_policies") or ["password_change"]
            ),
        }
        if not payload.get("password"):
            result["generated_password"] = "wire-generated-secret-2"
        return result

    return function_seam(services.ADMIN_RESET_PASSWORD, provider)


def auth_login_grant():
    """``auth.issue_login_grant`` — the claim step's mint."""
    from stapel_workspaces import services

    return function_seam(
        services.ISSUE_LOGIN_GRANT, {"grant_token": "wire-grant-token-1", "created": True}
    )


def profiles_set_display_name():
    """``profiles.set_display_name`` — the canonical name write.

    ``MemberNameView`` answers 503 when this is unreachable, on purpose, so
    the endpoint cannot be driven at all without standing it up.
    """
    from stapel_workspaces import services

    return function_seam(services.SET_DISPLAY_NAME, lambda payload: {"ok": True})


# ─────────────────────────────────────────────────────────────────────────────
# The recipe table
# ─────────────────────────────────────────────────────────────────────────────


class Call:
    """Performs one declared operation, and refuses to guess a path parameter."""

    def __init__(self, method, path):
        self.method = method
        self.path = path

    def __call__(self, client, params=None, data=None, query="", **extra):
        url = self.path
        for name, value in (params or {}).items():
            url = url.replace("{%s}" % name, str(value))
        assert "{" not in url, (
            f"{self.method} {self.path}: a path parameter this gate does not "
            "know how to fill — teach its recipe, or the operation goes unchecked"
        )
        send = getattr(client, self.method.lower())
        if self.method == "GET":
            return send(url + query, **extra)
        return send(url + query, data if data is not None else {}, format="json", **extra)


#: How to perform each operation the contract declares with a JSON response
#: body, keyed by ``(METHOD, path template, status code)``. ``code`` is
#: ``None`` for the usual case of one 2xx per operation; an explicit integer
#: would name one of several declared codes.
RECIPES = {}

#: The same operations again, in the emptiest state the contract still has to
#: describe: no rows, or the one row the operation addresses carrying none of
#: its optional values. A populated answer cannot say what a field holds when
#: there is nothing to hold, and that is where every null finding in the first
#: wave of this gate was.
EMPTY_STATE = {}


def recipe(method, path, code=None, table=None):
    def register(fn):
        target = RECIPES if table is None else table
        key = (method, V1 + path, code)
        assert key not in target, f"duplicate recipe for {method} {path} {code}"
        target[key] = fn
        return fn

    return register


def empty_state(method, path, code=None):
    return recipe(method, path, code, table=EMPTY_STATE)


#: Operations that cannot be driven in-process, by name and with the reason.
#: A short, visible list is acceptable here; a silent skip is not.
#:
#: EMPTY. Every operation this module declares is reachable from a test
#: client, including the four that call a sibling module by name — those are
#: comm Function SEAMS a deployment wires, so the gate wires them too and
#: everything on this side of the seam runs for real.
UNDRIVABLE: dict = {}


# ── the caller's own workspaces ──────────────────────────────────────────────


@recipe("GET", "/")
def _workspace_list(call):
    owner = make_user()
    make_workspace(owner, name="Wire Contract Org")
    return call(client_for(owner))


@empty_state("GET", "/")
def _workspace_list_empty(call):
    """A guest: no membership anywhere, so ``workspaces`` is [] and
    ``is_guest`` is the flag the frontend draws the empty dashboard from."""
    return call(client_for(make_user()))


@recipe("POST", "/", code=201)
def _workspace_create(call):
    return call(
        client_for(make_user()),
        data={"name": "Wire Contract Org", "slug": _unique("wire-org-"), "type": "work"},
    )


@recipe("GET", "/{workspace_id}")
def _workspace_detail(call):
    owner = make_user()
    workspace = make_workspace(owner)
    make_member(workspace)
    return call(client_for(owner), params={"workspace_id": workspace.id})


@empty_state("GET", "/{workspace_id}")
def _workspace_detail_empty(call):
    """A workspace nobody has touched: no settings block, no MFA policy —
    so ``mfa_enforcement`` is the null the claim says it may be."""
    owner = make_user()
    workspace = make_workspace(owner)
    return call(client_for(owner), params={"workspace_id": workspace.id})


@recipe("PATCH", "/{workspace_id}")
def _workspace_patch(call):
    owner = make_user()
    workspace = make_workspace(owner)
    return call(
        client_for(owner),
        params={"workspace_id": workspace.id},
        data={"name": "Wire Contract Org Renamed"},
    )


# ── roster ───────────────────────────────────────────────────────────────────


@recipe("GET", "/{workspace_id}/members")
def _member_list(call):
    owner = make_user()
    workspace = make_workspace(owner)
    make_member(workspace)
    return call(client_for(owner), params={"workspace_id": workspace.id})


@empty_state("GET", "/{workspace_id}/members")
def _member_list_empty(call):
    """The owner alone, never having opened the workspace: the row carries a
    null ``last_accessed_at``, a null ``display_name`` (no profile, no invite
    hint) and a null ``mfa_compliant`` (nobody has asked)."""
    owner = make_user()
    workspace = make_workspace(owner)
    return call(client_for(owner), params={"workspace_id": workspace.id})


@recipe("PATCH", "/{workspace_id}/members/{user_id}")
def _member_role_change(call):
    from stapel_workspaces.models import Role

    owner = make_user()
    workspace = make_workspace(owner)
    member = make_member(workspace, role=Role.MEMBER)
    return call(
        client_for(owner),
        params={"workspace_id": workspace.id, "user_id": member.user_id},
        data={"role": Role.ADMIN},
    )


@recipe("PATCH", "/{workspace_id}/members/{user_id}/name")
def _member_name(call):
    owner = make_user()
    workspace = make_workspace(owner)
    member = make_member(workspace)
    with profiles_set_display_name():
        return call(
            client_for(owner),
            params={"workspace_id": workspace.id, "user_id": member.user_id},
            data={"display_name": "Ada Lovelace"},
        )


@empty_state("PATCH", "/{workspace_id}/members/{user_id}/name")
def _member_name_cleared(call):
    """Clearing the name — the declared answer is the empty string, not null."""
    owner = make_user()
    workspace = make_workspace(owner)
    member = make_member(workspace)
    with profiles_set_display_name():
        return call(
            client_for(owner),
            params={"workspace_id": workspace.id, "user_id": member.user_id},
            data={"display_name": ""},
        )


@recipe("POST", "/{workspace_id}/members/{user_id}/password/reset")
def _member_password_reset(call):
    owner = make_user()
    workspace = make_workspace(owner)
    member = make_member(workspace)
    # Only an account the workspace provisioned is resettable.
    member.provisioned = True
    member.save(update_fields=["provisioned"])
    step_up(owner)
    with auth_password_reset():
        return call(
            client_for(owner),
            params={"workspace_id": workspace.id, "user_id": member.user_id},
            data={"reason": "wire-contract"},
        )


@empty_state("POST", "/{workspace_id}/members/{user_id}/password/reset")
def _member_password_reset_chosen(call):
    """The admin chose the password, so nothing is generated — the declared
    ``generated_password`` is null on this branch and only this one."""
    owner = make_user()
    workspace = make_workspace(owner)
    member = make_member(workspace)
    # Only an account the workspace provisioned is resettable.
    member.provisioned = True
    member.save(update_fields=["provisioned"])
    step_up(owner)
    with auth_password_reset():
        return call(
            client_for(owner),
            params={"workspace_id": workspace.id, "user_id": member.user_id},
            data={"password": "chosen-by-the-admin-9"},
        )


@recipe("POST", "/{workspace_id}/members/invite", code=201)
def _member_invite(call):
    owner = make_user()
    workspace = make_workspace(owner)
    return call(
        client_for(owner),
        params={"workspace_id": workspace.id},
        data={
            "emails": [f"{_unique('invitee-')}@example.com"],
            "role": "member",
            "display_name": "Ada Lovelace",
        },
    )


@empty_state("POST", "/{workspace_id}/members/invite", code=201)
def _member_invite_bare(call):
    """No name hint: the invitation rows come back with a null
    ``display_name``, a null ``accepted_at``/``declined_at``/``revoked_at``
    and — where no letter could be sent — a null ``last_sent_at``."""
    owner = make_user()
    workspace = make_workspace(owner)
    return call(
        client_for(owner),
        params={"workspace_id": workspace.id},
        data={"emails": [f"{_unique('invitee-')}@example.com"]},
    )


@recipe("POST", "/{workspace_id}/members/provision", code=201)
def _member_provision(call):
    owner = make_user()
    workspace = make_workspace(owner)
    step_up(owner)
    with auth_provision():
        return call(
            client_for(owner),
            params={"workspace_id": workspace.id},
            data={"username_local": _unique("jdoe"), "role": "member"},
        )


@empty_state("POST", "/{workspace_id}/members/provision", code=201)
def _member_provision_chosen_password(call):
    """The admin chose the password — ``generated_password`` is null here."""
    owner = make_user()
    workspace = make_workspace(owner)
    step_up(owner)
    with auth_provision():
        return call(
            client_for(owner),
            params={"workspace_id": workspace.id},
            data={
                "username_local": _unique("jdoe"),
                "role": "member",
                "password": "chosen-by-the-admin-9",
            },
        )


# ── the admin-side invitation surface ────────────────────────────────────────


@recipe("GET", "/{workspace_id}/invitations")
def _invitation_list(call):
    owner = make_user()
    workspace = make_workspace(owner)
    make_invitation(workspace, owner, display_name="Ada Lovelace")
    return call(client_for(owner), params={"workspace_id": workspace.id})


@empty_state("GET", "/{workspace_id}/invitations")
def _invitation_list_empty(call):
    owner = make_user()
    workspace = make_workspace(owner)
    return call(client_for(owner), params={"workspace_id": workspace.id})


@recipe("POST", "/{workspace_id}/invitations/{invitation_id}/revoke")
def _invitation_revoke(call):
    owner = make_user()
    workspace = make_workspace(owner)
    invitation = make_invitation(workspace, owner)
    return call(
        client_for(owner),
        params={"workspace_id": workspace.id, "invitation_id": invitation.id},
    )


@recipe("POST", "/{workspace_id}/invitations/{invitation_id}/resend")
def _invitation_resend(call):
    owner = make_user()
    workspace = make_workspace(owner)
    invitation = make_invitation(workspace, owner)
    return call(
        client_for(owner),
        params={"workspace_id": workspace.id, "invitation_id": invitation.id},
    )


@recipe("PATCH", "/{workspace_id}/invitations/{invitation_id}/name")
def _invitation_name(call):
    owner = make_user()
    workspace = make_workspace(owner)
    invitation = make_invitation(workspace, owner)
    return call(
        client_for(owner),
        params={"workspace_id": workspace.id, "invitation_id": invitation.id},
        data={"display_name": "Ada Lovelace"},
    )


@empty_state("PATCH", "/{workspace_id}/invitations/{invitation_id}/name")
def _invitation_name_cleared(call):
    owner = make_user()
    workspace = make_workspace(owner)
    invitation = make_invitation(workspace, owner, display_name="Typo")
    return call(
        client_for(owner),
        params={"workspace_id": workspace.id, "invitation_id": invitation.id},
        data={"display_name": ""},
    )


# ── the invitee's public surface ─────────────────────────────────────────────


@recipe("GET", "/invitations/{token}")
def _invitation_preview(call):
    owner = make_user()
    workspace = make_workspace(owner)
    invitee = make_user()
    invitation = make_invitation(workspace, owner, email=invitee.email)
    return call(anonymous(), params={"token": invitation.token})


@empty_state("GET", "/invitations/{token}")
def _invitation_preview_unregistered(call):
    """An address with no account behind it — ``email_registered`` false is
    the branch that routes the frontend to claim rather than to login."""
    owner = make_user()
    workspace = make_workspace(owner)
    invitation = make_invitation(workspace, owner)
    return call(anonymous(), params={"token": invitation.token})


@recipe("POST", "/invitations/{token}/claim")
def _invitation_claim(call):
    owner = make_user()
    workspace = make_workspace(owner)
    invitation = make_invitation(workspace, owner)
    with auth_login_grant():
        return call(anonymous(), params={"token": invitation.token})


@recipe("POST", "/invitations/accept")
def _invitation_accept(call):
    owner = make_user()
    workspace = make_workspace(owner)
    invitee = make_user()
    invitation = make_invitation(workspace, owner, email=invitee.email)
    return call(client_for(invitee), data={"token": invitation.token})


# ── the caller's stated home ─────────────────────────────────────────────────


@recipe("PUT", "/me/preferred-workspace")
def _preferred_put(call):
    owner = make_user()
    workspace = make_workspace(owner)
    return call(client_for(owner), data={"workspace_id": str(workspace.id)})


@recipe("DELETE", "/me/preferred-workspace")
def _preferred_delete(call):
    owner = make_user()
    workspace = make_workspace(owner)
    client = client_for(owner)
    assert client.put(
        f"{V1}/me/preferred-workspace",
        {"workspace_id": str(workspace.id)},
        format="json",
    ).status_code == 200
    return call(client)


@empty_state("DELETE", "/me/preferred-workspace")
def _preferred_delete_never_set(call):
    """Clearing a choice nobody made — idempotent, and the id comes back ""."""
    return call(client_for(make_user()))


# ── history ──────────────────────────────────────────────────────────────────


@recipe("GET", "/{workspace_id}/audit")
def _audit(call):
    owner = make_user()
    workspace = make_workspace(owner)
    make_invitation(workspace, owner)
    return call(client_for(owner), params={"workspace_id": workspace.id})


@empty_state("GET", "/{workspace_id}/audit")
def _audit_empty(call):
    """A workspace whose history has not started — ``create_workspace``
    writes no line, so this is a genuinely empty journal."""
    owner = make_user()
    workspace = make_workspace(owner)
    return call(client_for(owner), params={"workspace_id": workspace.id})


# ── deployment metadata ──────────────────────────────────────────────────────


@recipe("GET", "/roles")
def _roles(call):
    return call(client_for(make_user()))


@recipe("GET", "/instance")
def _instance(call):
    return call(anonymous())


# ── service-to-service ───────────────────────────────────────────────────────


@recipe("GET", "/internal/{workspace_id}/members/{user_id}")
def _internal_membership(call):
    owner = make_user()
    workspace = make_workspace(owner)
    member = make_member(workspace)
    return call(
        staff_client(),
        params={"workspace_id": workspace.id, "user_id": member.user_id},
    )


@empty_state("GET", "/internal/{workspace_id}/members/{user_id}")
def _internal_membership_untouched(call):
    """The row a service reads about somebody who has never signed in: null
    ``last_accessed_at``, null ``display_name``, null ``mfa_compliant``, and
    ``is_self`` false because the caller is a service and nobody's self."""
    owner = make_user()
    workspace = make_workspace(owner)
    return call(
        staff_client(),
        params={"workspace_id": workspace.id, "user_id": owner.id},
    )


@recipe("POST", "/internal/users/{user_id}/personal")
def _internal_personal_workspace(call):
    return call(staff_client(), params={"user_id": make_user().id})


# ─────────────────────────────────────────────────────────────────────────────
# The gate
# ─────────────────────────────────────────────────────────────────────────────


#: Operations whose declared body the wire does not send.
#:
#: EMPTY, and that is the finding rather than the absence of one: 24 of 24
#: operations were driven in both states and every declared body held. The
#: mechanism stays because the next change will need it — an entry must name
#: the defect AND its owner, and ``strict=True`` turns a fixed one into a
#: failure until the entry is deleted, so a finding can be neither forgotten
#: nor quietly kept.
KNOWN_MISMATCHES: dict = {}


def _recipe_for(table, method, path, code):
    """The code-specific recipe if there is one, else the operation's."""
    return table.get((method, path, code)) or table.get((method, path, None))


def test_the_contract_declares_something_to_check():
    assert OPERATIONS, "docs/schema.json declares no JSON responses at all"


def test_every_declared_path_resolves_under_this_urlconf():
    """The suite must be looking where the document describes.

    Three of the first four libraries this gate was written for had a
    committed contract that nothing had ever driven, because the test urlconf
    mounted somewhere the document does not describe: one mounted a different
    prefix AND one segment short, one mounted the paths bare, and one mounted
    less than the emission did. THIS module is a fourth: ``conftest_urls.py``
    mounts ``workspaces/api/workspaces/``, a doubled segment reproducing one
    host's deployed prefix, under which not one path of the committed
    document resolves.

    That is the same family as a gate nobody asks: the recipes can all be
    written, the run can be green, and not one request went where the
    contract says it goes. A missing recipe already fails loudly; this fails
    when the MOUNT is wrong, which no per-operation check can see, because
    when the mount is wrong every operation is equally and silently
    unreachable.

    Asserted against the urlconf this module declares, so it fails at the one
    moment it is cheap to fix: when somebody changes a mount.
    """
    from django.urls import Resolver404, resolve

    # Resolution cares about the SHAPE of a segment, and this URL set uses
    # several converters — uuid, int, str. A path counts as reachable if any
    # one shape resolves: the question here is whether the mount exists, not
    # whether a particular id does.
    candidates = (
        "00000000-0000-4000-8000-000000000000",
        "1",
        "a-slug",
    )

    unreachable = []
    for _method, path, _code, _schema in OPERATIONS:
        for value in candidates:
            try:
                resolve(re.sub(r"\{[^}]+\}", value, path))
                break
            except Resolver404:
                continue
        else:
            unreachable.append(path)

    assert not unreachable, (
        "these declared paths do not resolve under this module's urlconf, so "
        "nothing here can be driving them — the mount is wrong, not the "
        "recipes:\n  " + "\n  ".join(sorted(set(unreachable)))
    )


def test_every_declared_operation_is_driven_or_named_undrivable():
    """No operation is covered by silence, and no entry outlives its operation."""
    missing = [
        (method, path, code)
        for method, path, code, _schema in OPERATIONS
        if _recipe_for(RECIPES, method, path, code) is None
        and (method, path) not in UNDRIVABLE
    ]
    assert not missing, (
        "operations with a declared JSON response body and no recipe:\n"
        + "\n".join(f"  {m} {p} -> {c}" for m, p, c in missing)
    )

    declared_codes = {(m, p, c) for m, p, c, _ in OPERATIONS}
    declared_ops = {(m, p) for m, p, _c, _ in OPERATIONS}
    stale = sorted(
        key
        for key in RECIPES
        if (key[0], key[1]) not in declared_ops
        or (key[2] is not None and key not in declared_codes)
    )
    assert not stale, (
        "recipes for operations/status codes the contract no longer declares:\n"
        + "\n".join(f"  {m} {p} -> {c}" for m, p, c in stale)
    )
    stale_exclusions = sorted(set(UNDRIVABLE) - declared_ops)
    assert not stale_exclusions, (
        f"exclusions for operations the contract no longer declares: {stale_exclusions}"
    )
    both = sorted((m, p) for m, p, _c in RECIPES if (m, p) in UNDRIVABLE)
    assert not both, f"driven AND excluded: {both}"
    for key, reason in UNDRIVABLE.items():
        assert reason and reason.strip(), f"{key} is excluded with no reason"


def test_every_read_is_also_driven_in_its_emptiest_state():
    """A populated answer cannot say what a field holds when there is nothing.

    Every null finding in the first wave of this gate was on the empty state.
    A gate that only ever seeds three rows and asks never sees any of them.

    So every GET is required to have an ``EMPTY_STATE`` recipe as well. The
    exemptions are named here, each with its reason.
    """
    exempt = {
        # Derived from BUILTIN_ROLES plus the deployment's overlay. There is
        # no per-caller or per-row state for it to be empty of, so an "empty"
        # run would be byte-for-byte the populated one.
        ("GET", V1 + "/roles"),
        # Derived from STREET_LANDING_MODE alone — same reason.
        ("GET", V1 + "/instance"),
    }
    reads = {
        (method, path)
        for method, path, _code, _schema in OPERATIONS
        if method == "GET"
    }
    covered = {(m, p) for m, p, _c in EMPTY_STATE}
    missing = sorted(reads - covered - exempt)
    assert not missing, (
        "reads driven only against a populated database — the state where "
        "every null claim in this gate's history was found is unchecked:\n"
        + "\n".join(f"  {m} {p}" for m, p in missing)
    )
    declared_ops = {(m, p) for m, p, _c, _ in OPERATIONS}
    stale = sorted({(m, p) for m, p, _c in EMPTY_STATE} - declared_ops)
    assert not stale, f"empty-state recipes for undeclared operations: {stale}"


def test_every_known_mismatch_is_still_declared_and_explained():
    """A recorded defect must name a live operation and carry its reason.

    Without this, an operation that is renamed or removed leaves an entry that
    silences nothing and reads like a known problem forever.
    """
    declared = {(method, path) for method, path, _code, _schema in OPERATIONS}
    for key, reason in KNOWN_MISMATCHES.items():
        assert key in declared, (
            f"{key} is recorded as a known mismatch but the contract no longer "
            "declares it — delete the entry"
        )
        assert reason and reason.strip(), f"{key} is recorded with no reason"


def _drive(table, method, path, code, body_schema, *, expect_rows):
    perform = _recipe_for(table, method, path, code)
    assert perform is not None, (
        f"{method} {path} declares a response body and has no recipe — an "
        "unchecked operation is a schema nobody proves. Teach RECIPES, or "
        "name it in UNDRIVABLE with a reason."
    )

    response = perform(Call(method, path))
    assert response.status_code == code, (
        f"{method} {path}: expected the declared {code}, got "
        f"{response.status_code}: {response.content[:400]}"
    )

    body = response.json()
    errors = sorted(_validator(body_schema).iter_errors(body), key=lambda e: list(e.path))
    assert not errors, (
        f"{method} {path} answers a body the contract does not describe:\n"
        + "\n".join(f"  at {list(e.path) or '<root>'}: {e.message}" for e in errors[:10])
        + f"\n  body: {json.dumps(body)[:600]}"
    )
    # An empty list validates against any item schema, so a collection must
    # actually carry a row for the check to have looked at anything — both the
    # bare arrays and the anchor-pagination envelope's ``items``.
    if expect_rows:
        rows = body if isinstance(body, list) else None
        if rows is None and isinstance(body, dict) and isinstance(body.get("items"), list):
            rows = body["items"]
        if rows is not None:
            assert rows, f"{method} {path}: the declared collection came back empty"
    return body


@pytest.mark.parametrize(
    "method,path,code,body_schema",
    OPERATIONS,
    ids=[f"{m} {p} {c}" for m, p, c, _ in OPERATIONS],
)
def test_the_wire_matches_the_declared_response(method, path, code, body_schema, request):
    if (method, path) in UNDRIVABLE:
        pytest.skip(f"excluded by name: {UNDRIVABLE[(method, path)]}")

    if (method, path) in KNOWN_MISMATCHES:
        request.node.add_marker(
            pytest.mark.xfail(
                strict=True,
                reason=f"{method} {path}: {KNOWN_MISMATCHES[(method, path)]}",
            )
        )

    _drive(RECIPES, method, path, code, body_schema, expect_rows=True)


_EMPTY_OPERATIONS = [
    (method, path, code, schema)
    for method, path, code, schema in OPERATIONS
    if _recipe_for(EMPTY_STATE, method, path, code) is not None
]


@pytest.mark.parametrize(
    "method,path,code,body_schema",
    _EMPTY_OPERATIONS,
    ids=[f"{m} {p} {c}" for m, p, c, _ in _EMPTY_OPERATIONS],
)
def test_the_wire_matches_the_declared_response_when_there_is_nothing_there(
    method, path, code, body_schema, request
):
    """The same claim, asked in the state where the nulls live."""
    if (method, path) in KNOWN_MISMATCHES:
        request.node.add_marker(
            pytest.mark.xfail(
                strict=True,
                reason=f"{method} {path}: {KNOWN_MISMATCHES[(method, path)]}",
            )
        )

    _drive(EMPTY_STATE, method, path, code, body_schema, expect_rows=False)


def test_the_gate_is_not_blind():
    """A canary: swap a declared schema for one the wire cannot satisfy.

    Everything above can be green for two reasons — the claims are honest, or
    the check never looks at the body. This tells them apart by validating a
    real response against ``{"type": "string"}``: every operation here answers
    an object, so every one of them must fail. If any passes, the validation
    in ``_drive`` is not reaching the received body and this whole file proves
    nothing. With ``KNOWN_MISMATCHES`` empty this covers the entire declared
    surface.
    """
    honest = [
        (method, path, code)
        for method, path, code, _schema in OPERATIONS
        if (method, path) not in KNOWN_MISMATCHES and (method, path) not in UNDRIVABLE
    ]
    assert honest, "nothing left to canary"

    survivors = []
    for method, path, code in honest:
        try:
            _drive(RECIPES, method, path, code, {"type": "string"}, expect_rows=False)
        except AssertionError:
            continue
        survivors.append(f"{method} {path}")
    assert not survivors, (
        "these operations passed validation against {'type': 'string'} — the "
        "gate is not looking at the body it received:\n  " + "\n  ".join(survivors)
    )
