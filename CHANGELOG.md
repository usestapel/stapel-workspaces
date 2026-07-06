# Changelog

## 0.3.8 — 2026-07-06

### Added — ru error catalog + bilingual error reference (i18n-shipping волна 2)

Reference-pattern application of the `stapel_core.i18n` catalog contour to the
`errors` domain (i18n-shipping.md §5), copied 1:1 from the stapel-auth pilot.

- `translations/errors.ru.json` — flat `{code: text}` ru catalog covering all
  52 keys, with `translations/.state.json` provenance sidecar. **50** keys
  seeded from the curated `stapel-translate` builtin fixtures (`origin:
  seed:stapel-builtin`, no tokens spent), **2** machine-translated (`origin:
  llm`, unreviewed). `translations/.errors.ru.llm-cache.json` is the
  committed, content-hash translation cache.
- `docs/errors.en.md` · `docs/errors.ru.md` — generated human-readable
  references; README + MODULE.md link both languages.
- `tests/test_error_i18n.py` — `check_translation_catalogs` gate + env-gated
  regen (`STAPEL_REGEN_ERROR_I18N=1`).


## 0.3.7 — 2026-07-06

### Added
- Declarative error registry with machine-readable remediation hints. Every
  `error.<status>.<name>` key the service raises now carries a `remediation`
  from the finite vocabulary (`retry | wait_and_retry | reauthenticate |
  verify | fix_input | contact_support | bug`), declared alongside the keys via
  `register_service_errors(..., remediation=...)`.
- `docs/errors.json` codegen artifact (`generate_error_keys`) — the
  language-agnostic registry of every key with its `status`, `{param}` slots,
  `remediation`, and English text — plus a byte-stable drift gate
  (`tests/test_error_keys.py`, mirrors the flow-doc/schema.json discipline).
- Canon remediation overrides where the frontend status+name heuristic lies
  (7 of 11 keys): `*_not_found` → `fix_input` (heuristic retries a 404,
  looping the lookup); `forbidden_workspace` → `contact_support` (not-a-member
  boundary — no field to fix, an owner must invite/promote); `last_owner…` →
  `fix_input` (self-serve precondition "transfer ownership first");
  `invitation_expired`/`invitation_revoked` → `contact_support` (dead,
  immutable token — only the owner can re-invite; the heuristic says retry for
  expired, which loops on a spent token). The reasoning is documented per key
  in `errors.py`.

### Changed
- Test settings now install `stapel_core.django.apps.CommonDjangoConfig` so the
  `generate_error_keys` management command is available to the drift gate.


## 0.3.6 — 2026-07-06

### Changed
- Pinned `stapel-core` to the `>=0.8,<0.9` window (library-standard §7.1: one
  minor window; floor `0.8.0` is published on PyPI — no pin into the void).
- CI: added the release-track job (library-standard §7.4) — installs the package
  the way an end user does (`pip install .`, dependencies resolved from PyPI
  strictly by the declared pins, no git-main core, no editable siblings), asserts
  `stapel-core` resolves inside the `0.8` window, and runs an import smoke.
  Advisory (continue-on-error) until the whole stapel graph is on PyPI; becomes
  the blocking precondition for a `vX.Y.Z` tag once it is.


## 0.3.5 — 2026-07-06

### Packaging
- Tests excluded from the built wheel/sdist (the `stapel_workspaces.tests`
  and `stapel_workspaces.tests.brownfield_users` subpackages are no longer
  listed in `[tool.setuptools] packages`). Added `[project.urls]`, completed
  the trove classifiers (MIT/OSI, Python 3.13, `Typing :: Typed`, OS
  Independent, `3 :: Only`, Development Status) and a `[tool.ruff]` lint
  section (single source shared with the git hooks/CI).


## 0.3.4 — 2026-07-05

### Changed
- OpenAPI: `@extend_schema` for `InternalPersonalWorkspaceView` (POST get-or-create
  personal workspace). Documents `request=None`, `200` →
  `InternalPersonalWorkspaceResponseSerializer` (`workspace_id`), `404` →
  `StapelErrorSerializer` — resolves the drf-spectacular "unable to guess
  serializer" error so the generated client is typed.

## 0.3.3 — 2026-07-05

### Fixed
- Reference `settings.AUTH_USER_MODEL` not the concrete `User` — unblocks
  custom user models / brownfield adoption. The `Workspace.owner`,
  `WorkspaceMember.user`, `WorkspaceMember.invited_by` and
  `WorkspaceInvitation.invited_by` FKs now target the swappable
  `settings.AUTH_USER_MODEL`, and `services`, `views` and
  `consume_auth_events` resolve the user via
  `django.contrib.auth.get_user_model()` instead of importing
  `stapel_core.django.users.models.User`. A host with a custom
  `accounts.User(AbstractStapelUser)` as `AUTH_USER_MODEL` no longer hits
  `ValueError: ... must be a "User" instance` when creating a
  `WorkspaceMember`. No migration/DB change: the FKs already deconstructed
  to `settings.AUTH_USER_MODEL` (the initial migration used
  `migrations.swappable_dependency`), so `makemigrations` reports no changes.


## 0.3.2 — 2026-07-05

### Fixed
- `user_id` in comm schemas typed uuid, was integer — rejected valid
  `user.deleted` events. `schemas/consumes/user.deleted.json` and
  `schemas/consumes/user.deletion_initiated.json` now type `user_id` as
  `{"type": "string", "format": "uuid"}`, matching the UUID-pk canonical
  user and the auth/gdpr producers.


## 0.3.1 — 2026-07-04

### Added
- `MODULE.md` — agent-facing extension-point map (part of the July 2026
  framework-wide documentation sweep). No functional changes.

## 0.3.0 — 2026-07-03

No functional changes — version alignment with the Stapel 0.3
release train; stapel-core dependency now `>=0.3.0,<0.4`.


## 0.2.1 — 2026-07-02

### Fixed
- `consume_auth_events` subscribes to the comm topic `user.registered`
  (the legacy Kafka topic it listened on is no longer published);
  personal-workspace bootstrap works again in microservices mode.
- `workspace.personal.created` published under its action name; legacy
  topic constant aliased.

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] - 2026-07-02

### Added
- comm Function provider `workspaces.check_membership` (`functions.py`),
  registered from `AppConfig.ready()`. Same semantics as the internal HTTP
  membership endpoint: payload `{"workspace_id": str, "user_id": str}` →
  `{"is_member": bool, "role": str | null}`.
- Declared events are now actually emitted through `stapel_core.comm.emit`
  (transactional outbox): `workspace.created` on `create_workspace`,
  `workspace.member_joined` on `accept_invitation` and the personal-workspace
  bootstrap, and `workspace.personal.created` on the personal-workspace
  bootstrap.
- Invitation delivery: `create_invitation` requests a best-effort
  `workspace.invitation` notification via
  `stapel_core.notifications.request_notification` (variables:
  `workspace_name`, `inviter_name`, `accept_url`; `accept_url` uses the
  `FRONTEND_URL` setting when configured). Failures are logged and never
  break invitation creation.
- `stapel_core.signals.workspace_member_changed` is sent on member add
  (workspace create, invitation accept), role change ("updated") and
  removal ("removed").
- Cross-service membership cache invalidation
  (`stapel_core.django.workspaces.invalidate_membership_cache`) on member
  role change, member removal and invitation accept.
- Payload schema `schemas/emits/workspace.personal.created.json` and
  function schema `schemas/functions/workspaces.check_membership.json`.
- `py.typed` marker (PEP 561).
- Tests for the comm function, emitted event payloads (validated against
  the schema files), cache invalidation and the membership signal.

### Changed
- `schemas/emits/workspace.created.json` and
  `schemas/emits/workspace.member_joined.json`: `workspace_id`,
  `owner_id`, `user_id` are string UUIDs (matching the real payloads,
  which serialize UUIDs to `str`), `type`/`role` are enums.

## [0.1.0]

### Added
- Initial release: workspaces, members and RBAC, invitations, internal
  service API, GDPR provider.
