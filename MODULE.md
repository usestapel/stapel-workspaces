# stapel-workspaces — MODULE.md

Agent-facing map of this module: what it provides, its fork-free extension points, and
anti-patterns. Use it to classify a desired change as **app-layer override via an
extension point** vs **upstream contribution** (see `docs/stdlib-contribution-pipeline.md`
and system-design.md §8.6 in the Stapel monorepo). Stapel modules never import each
other; all cross-module communication goes through `stapel-core` (comm bus, signals,
registries). Everything below is verifiable against the code in this repo.

- Package: `stapel-workspaces` (PyPI), Python package `stapel_workspaces`, Django app label `workspaces`.
- Depends on `stapel-core` only (`>=0.3.0,<0.4`; plus DRF, drf-spectacular via core).
- This is the foundational tenancy service: workspace-scoped resources in other modules
  carry a `workspace_id` pointing at the `Workspace` row owned here.

## What this module provides

| Area | Contents |
|---|---|
| Models (`models.py`) | `Workspace` (UUID pk, `name`, unique `slug`, `type` personal\|work, `owner` FK `PROTECT`, JSON `settings`, `storage_used_bytes` / `storage_limit_bytes` default 5 GiB, soft-delete via `deleted_at`; `db_table workspaces_workspace`), `WorkspaceMember` (unique `(workspace, user)`, `role`, `invited_by`, `invited_at` / `accepted_at` / `last_accessed_at`; `db_table workspaces_member`), `WorkspaceInvitation` (email invite, unique single-use `token`, `expires_at`, `accepted_at` / `revoked_at`; `db_table workspaces_invitation`). Enums: `WorkspaceType` (`personal`/`work`), `Role` (`owner`/`admin`/`member`/`viewer`) |
| Services (`services.py`) | `create_workspace()` (atomic; seeds OWNER membership, emits `workspace.created`, sends `workspace_member_changed`), `ensure_personal_workspace()` (get-or-create on first login; emits `workspace.personal.created` + `workspace.member_joined`), `create_invitation()` (7-day token; best-effort `request_notification("workspace.invitation", ...)` via `stapel_core.notifications`), `accept_invitation()` (row-locked single-use token → membership; emits `workspace.member_joined`, invalidates the cross-service membership cache, sends signal) |
| Permissions (`permissions.py`) | `ROLE_HIERARCHY = [VIEWER, MEMBER, ADMIN, OWNER]`, `role_at_least()`, `get_membership()` (accepted memberships only), `require_role()` |
| HTTP API (`urls.py`, `views.py`) | Workspace list/create, detail (GET/PATCH/DELETE soft-delete), member list, invite, member role change / removal (last-owner protected), invitation accept, plus internal service-to-service endpoints (`IsServiceRequest \| IsStaffUser`): membership lookup and personal-workspace get-or-create |
| comm Function (`functions.py`) | `workspaces.check_membership` (constant `CHECK_MEMBERSHIP`), registered idempotently in `AppConfig.ready()` with `CHECK_MEMBERSHIP_SCHEMA` |
| Events (`events.py`, `schemas/`) | `EVENT_WORKSPACE_PERSONAL_CREATED = "workspace.personal.created"`, `WorkspacePersonalCreatedPayload` dataclass, `EVENT_REGISTRY`; JSON Schemas in `schemas/emits/`, `schemas/consumes/`, `schemas/functions/` |
| Bus consumer (`management/commands/consume_auth_events.py`) | Listens on `user.registered` (consumer group `workspaces-auth-events`) → `ensure_personal_workspace()` → publishes `workspace.personal.created` |
| GDPR (`gdpr.py`, `apps.py`) | `WorkspacesGDPRProvider` (section `"workspaces"`), registered with `stapel_core.gdpr.gdpr_registry` in `AppConfig.ready()`; export (memberships, owned workspaces, sent invites), delete (memberships removed, pending sent invites deleted, owned workspaces **soft**-deleted), anonymize (`invited_by` cleared on accepted invites) |
| Errors (`errors.py`) | `WORKSPACES_ERRORS` keys (`error.404.workspace_not_found`, `error.403.forbidden_workspace`, `error.403.last_owner_cannot_be_removed`, `error.400.invitation_expired`, ...) registered via `register_service_errors`; `WorkspacesErrorKeysView` |
| Admin (`admin.py`) | `ModelAdmin`s for all three models (invitation `token` read-only) |
| Public API (`__init__.py`, PEP 562 lazy) | `__all__ = ["create_workspace", "ensure_personal_workspace", "create_invitation", "accept_invitation", "CHECK_MEMBERSHIP", "check_membership", "EVENT_WORKSPACE_PERSONAL_CREATED", "WorkspacesGDPRProvider"]` |

Consumer-side helpers live in **stapel-core**, not here: `stapel_core.django.workspaces`
(`get_membership`, `require_role`, `invalidate_membership_cache`,
`get_or_create_personal_workspace` — HTTP against the internal API with a 30 s cache) and
`stapel_core.comm.call("workspaces.check_membership", ...)`. Other modules use those;
they never import `stapel_workspaces`.

## Extension points (fork-free)

### Settings

This module has **no `AppSettings` namespace** (no `conf.py`; there is no
`STAPEL_WORKSPACES` setting — unlike e.g. `stapel-billing`). What is configurable today:

| Key | Kind | Default | What it customizes |
|---|---|---|---|
| `FRONTEND_URL` | flat Django setting | `""` | Base URL for the invitation accept link (`{FRONTEND_URL}/invitations/{token}/accept`) in the invite notification (`services._send_invitation_notification`) |
| `STAPEL_TOPIC_USER_REGISTERED` | env var | `"user.registered"` | Topic the `consume_auth_events` command subscribes to (legacy topic layouts) |
| `STAPEL_COMM` / `STAPEL_BUS_BACKEND` | core namespaces (`stapel_core.comm.config`, `stapel_core.bus`) | — | Transport for all emits/consumes/function calls (in-process in a monolith, bus in microservices) — deployment config, not code |
| `WORKSPACES_SERVICE_URL`, `SERVICE_API_KEY` | env vars (consumer side, in `stapel_core.django.workspaces`) | `http://stapel-workspaces:8000`, `""` | Where other services reach the internal membership API, and the `X-API-KEY` they present |

**Not configurable today** (hard-coded; making any of them a setting is an upstream
contribution): invitation expiry (7 days, `services.create_invitation`), default storage
quota (5 GiB, `Workspace.storage_limit_bytes`), slug auto-generation
(`services._make_unique_slug`), the role set and hierarchy (see below), the 30 s
consumer-side membership cache TTL (`stapel_core.django.workspaces.CACHE_TTL_SECONDS`).

### Swappable models

None. `Workspace`, `WorkspaceMember`, `WorkspaceInvitation` are not swappable and have
fixed `db_table` names (`workspaces_*`). The user binding follows standard Django: the
FKs (`Workspace.owner`, `WorkspaceMember.user`/`invited_by`,
`WorkspaceInvitation.invited_by`) target `settings.AUTH_USER_MODEL`, and runtime code
resolves the user via `django.contrib.auth.get_user_model()` — never the concrete
`stapel_core.django.users.models.User`. Host projects extend the user by subclassing
`AbstractStapelUser` and pointing `AUTH_USER_MODEL` at it (see
`stapel_core.django.users.models`).

To attach extra per-workspace data without a fork:

- `Workspace.settings` — a JSON bag, PATCHable through the workspace API
  (`WorkspaceUpdateRequest.settings`); the sanctioned place for app-level workspace
  preferences.
- An app-layer side table with a FK/OneToOne to `workspaces_workspace` /
  `workspaces_member`.

New columns, indexes, or constraints on these tables = upstream contribution
(migrations live in this repo).

### Roles / permissions customization

The role set is a **fixed contract**, deliberately mirrored in three places that must
stay in sync: `models.Role` (TextChoices), `permissions.ROLE_HIERARCHY`, and
`stapel_core.django.workspaces.ROLE_HIERARCHY` (plus the `role` enum in
`schemas/emits/workspace.member_joined.json`). It is not settings-configurable —
adding/renaming roles or reordering the hierarchy is an upstream contribution.

What **is** app-layer:

- Mapping roles to app-specific capabilities in your own code via
  `stapel_workspaces.permissions.role_at_least` / `require_role` (in-service) or
  `stapel_core.django.workspaces.require_role` / `comm.call("workspaces.check_membership")`
  (from any other service, no import of this app).
- Enforced invariants you can rely on (and must not re-implement loosely): only owners
  may grant/revoke the OWNER role; the last owner cannot be demoted or removed
  (`error.403.last_owner_cannot_be_removed`); only *accepted* memberships count for
  access checks and for `check_membership`.

### Serializer seams (`views.py`)

Every public view mixes in `SerializerSeamsMixin` with class attributes
`request_serializer_class` / `response_serializer_class` and overridable getters
`get_request_serializer_class()` / `get_response_serializer_class()`
(`WorkspaceListCreateView` adds `list_response_serializer_class` +
`get_list_response_serializer_class()`). To change a payload shape: subclass the view,
swap the class attribute (serializers are `StapelDataclassSerializer`s over the
dataclasses in `dto.py` — pair a new serializer with a new dataclass), and mount your
subclass in the host URLconf instead of the stock route. HTTP method bodies stay
untouched.

| View | Route (name) | Request serializer | Response serializer |
|---|---|---|---|
| `WorkspaceListCreateView` | `""` — mount root (`workspace-list`) | `WorkspaceCreateRequestSerializer` | `WorkspaceResponseSerializer`; list: `WorkspaceListResponseSerializer` |
| `WorkspaceDetailView` | `<uuid:workspace_id>` (`workspace-detail`) | `WorkspaceUpdateRequestSerializer` | `WorkspaceResponseSerializer` |
| `MemberListView` | `<ws>/members` (`workspace-members`) | — | `MemberListResponseSerializer` |
| `MemberInviteView` | `<ws>/members/invite` (`workspace-member-invite`) | `MemberInviteRequestSerializer` | `MemberInviteResponseSerializer` |
| `MemberDetailView` | `<ws>/members/<user_id>` (`workspace-member-detail`) | `MemberUpdateRequestSerializer` | `MemberResponseSerializer` |
| `InvitationAcceptView` | `invitations/accept` (`workspace-invitation-accept`) | `InvitationAcceptRequestSerializer` | `MemberResponseSerializer` |
| `InternalMembershipView` | `internal/<ws>/members/<user_id>` (`workspace-internal-membership`) | — | `MemberResponseSerializer` |
| `InternalPersonalWorkspaceView` | `internal/users/<user_id>/personal` (`workspace-internal-personal`) | — | — (plain dict; no seam mixin) |

### Events & functions (comm surface)

Transport-agnostic via `stapel_core.comm` (`emit` uses the transactional outbox: an
event leaves iff the surrounding DB transaction commits). JSON Schemas in `schemas/`.

**Emits** (from `services.py`):

| Event | Payload (required) | When |
|---|---|---|
| `workspace.created` | `workspace_id`, `owner_id`, `name`, `type` | Every workspace creation (owner membership seeded in the same transaction) |
| `workspace.member_joined` | `workspace_id`, `user_id`, `role` | Invitation accepted; owner seeded at personal-workspace bootstrap. Re-emitted for already-existing memberships — subscribers must be idempotent |
| `workspace.personal.created` | `workspace_id`, `user_id` | Personal workspace auto-created (constant `EVENT_WORKSPACE_PERSONAL_CREATED`; payload dataclass `WorkspacePersonalCreatedPayload`) |

**Consumes** (`actions.py`, `@on_action`; handlers must be idempotent — delivery is
at-least-once):

| Event | Handler | Effect |
|---|---|---|
| `user.deleted` | `handle_user_deleted` | `WorkspacesGDPRProvider().delete(user_id)` — memberships removed, owned workspaces soft-deleted |

(`schemas/consumes/user.deletion_initiated.json` is declared, but `actions.py` currently
subscribes only to `user.deleted`.)

Additionally, the `consume_auth_events` management command (bus deployments) consumes
`user.registered` and bootstraps the personal workspace.

**Functions provided:**

| Function | Payload | Returns | Notes |
|---|---|---|---|
| `workspaces.check_membership` (`CHECK_MEMBERSHIP`) | `{"workspace_id": uuid-str, "user_id": uuid-str}` | `{"is_member": bool, "role": str \| null}` | Only *accepted* memberships count. Mirrors the internal HTTP endpoint (`InternalMembershipView`). Call via `stapel_core.comm.call` — never import this app |

### Django signals

Defined in `stapel_core.signals` (in-process only, no delivery guarantees — host
projects connect receivers freely; cross-service reactions must use the comm events):

| Signal | Sender | Kwargs | Sent from |
|---|---|---|---|
| `workspace_member_changed` | `WorkspaceMember` | `workspace`, `user`, `role`, `action` (`"added"` \| `"updated"` \| `"removed"`) | `services.create_workspace` (owner seed), `services.accept_invitation` (added), `views.MemberDetailView.patch` (updated), `views.MemberDetailView.delete` (removed) |

**Error localization** (i18n-shipping.md §5): `docs/errors.json` is the existing
en canon codegen artifact (the array of `{code, status, params, remediation,
en}` entries emitted by core's `generate_error_keys` from `errors.py`'s
`register_service_errors` call, plus the cross-cutting `verification`/`captcha`
keys). ru ships as a flat `translations/errors.ru.json` catalog with a
`translations/.state.json` provenance sidecar, and human-readable references
[Errors (EN)](docs/errors.en.md) · [Ошибки (RU)](docs/errors.ru.md). Semantics of
the i18n seams (library-standard §3.3 — MODULE.md states the merge semantics of
each key): the **error registry** is `dict.update`/**last-wins** (a host
`errors.py` autodiscovered after ours overrides an en text — and its raise-time
render — without a fork); the **locale catalogs** are discovered over
INSTALLED_APPS and merged **later-wins** (a host app's
`translations/errors.<lang>.json` overrides our texts, and an override MUST keep
the canon's `{param}` slots — gated). ru provenance is honest: 50 keys seeded
from the curated `stapel-translate` builtin fixtures (`origin: seed:stapel-builtin`,
no tokens spent), 2 keys machine-translated (`origin: llm`, unreviewed — the
gate's W-counter, cleared by `translate_catalogs --approve`). Gate + regenerate:
`tests/test_error_i18n.py` (`check_translation_catalogs` — E on
missing/stale/params/byte-instability); regenerate with
`STAPEL_REGEN_ERROR_I18N=1 pytest tests/test_error_i18n.py::test_regen` and commit
`translations/errors.ru.json`, `translations/.state.json`, `docs/errors.{en,ru}.md`.

## Anti-patterns

- **Don't import `stapel_workspaces` from another Stapel module** (and don't import
  other `stapel-*` modules here). Membership checks from elsewhere go through
  `comm.call("workspaces.check_membership")` or `stapel_core.django.workspaces`
  helpers; reactions go through the comm events or the signal.
- **Don't create/mutate `Workspace` / `WorkspaceMember` rows with raw ORM writes.**
  Use `services.create_workspace` / `accept_invitation` (or the HTTP API). They are
  atomic, emit the outbox events, send `workspace_member_changed`, and call
  `invalidate_membership_cache` — direct writes leave other services with a stale 30 s
  membership cache and skip every subscriber.
- **Don't hard-delete workspaces.** Deletion is soft (`deleted_at`); `Workspace.owner`
  is `on_delete=PROTECT` and GDPR erasure also soft-deletes. All queries here filter
  `deleted_at__isnull=True` — app-layer code must too.
- **Don't loosen the invitation contract.** Tokens are single-use (enforced with
  `select_for_update` in `accept_invitation`) and personal — `InvitationAcceptView`
  rejects a token whose email doesn't match `request.user.email`. A custom accept flow
  must keep both properties.
- **Don't re-implement the role hierarchy ad hoc.** Use `role_at_least` /
  `require_role` (or the core consumer-side helpers). In particular, keep the
  owner-only checks: any admin being able to grant OWNER, or the last owner being
  removable, breaks the module's invariants.
- **Don't rewrite view bodies to change payload shapes.** Use the serializer seam
  (subclass + `request_serializer_class` / `response_serializer_class` + remount the
  URL).
- **Don't fork to add workspace fields.** Use the `Workspace.settings` JSON bag or an
  app-layer side table; schema changes to `workspaces_*` tables are upstream.
- **Don't write non-idempotent subscribers.** Delivery is at-least-once, and
  `workspace.member_joined` is deliberately re-emitted for already-existing
  memberships.
- **Don't make invitation delivery load-bearing.** The invite notification is
  best-effort by design (`_send_invitation_notification` swallows failures); the
  invitation row is the source of truth — list/resend from it.

## App-layer override vs upstream contribution — rule of thumb

**App-layer override** (client-owned, no fork) when the change fits an extension point
above: reacting to workspace/membership changes (receivers on
`workspace_member_changed`, subscribers on `workspace.created` /
`workspace.member_joined` / `workspace.personal.created`), calling
`workspaces.check_membership` from other code, request/response payload shapes
(serializer seams + URL remount), extra per-workspace data (`Workspace.settings` JSON
or a side table with a FK), the invite link base (`FRONTEND_URL`), the invite email's
template/wording (the `"workspace.invitation"` notification is rendered by the
notifications stack, not here), transport and topics (`STAPEL_COMM`,
`STAPEL_TOPIC_USER_REGISTERED`).

**Upstream contribution** (Stapel-owned, via the contribution pipeline) when the change
alters module-owned contracts or invariants: new roles or hierarchy changes (enum +
mirrors + schema), fields/indexes/migrations on the `workspaces_*` tables, new or
changed emitted events and their schemas, new endpoints or comm functions,
invitation/last-owner/soft-delete logic, making hard-coded values configurable
(invitation expiry, storage quota, cache TTL — no seam exists today), introducing a
`STAPEL_WORKSPACES` `AppSettings` namespace, subscribing to `user.deletion_initiated`,
bug fixes anywhere in this repo.

If a needed seam does not exist, the seam itself is an upstream contribution; the code
that plugs into it stays app-layer.
