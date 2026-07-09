# Changelog

## [Unreleased]

### Added — per-module contract emission: `schema` + `flows` triad (contract-pipeline.md Wave 1)

stapel-workspaces now emits its **own** API contract per-module, completing the
triad `docs/{schema,flows,errors}.json` (`errors.json` already existed). The
frontend codegen can now read workspaces' committed artifacts instead of the
monolith aggregate at floating `main` — contract-pipeline.md verdict **A**
(contract = a reviewable, version-pinned commit). Copies the stapel-auth ETALON
harness (`_codegen_settings.py` / `codegen_urls.py` / `_codegen.py` / `Makefile`
/ `tests/test_contract.py`), adapted for this module's shape.

- **Harness** (reuses `stapel_tools.codegen`, adds ~90 lines of per-module config):
  - `_codegen_settings.py` — single source of truth for the `settings.configure`
    block, shared with `conftest.py` (extracted, no test-behavior change); a
    `contract=True` mode swaps in the production `REST_FRAMEWORK`.
  - `codegen_urls.py` — mounts `stapel_workspaces.urls` alone at the canonical
    `workspaces/api/` prefix (exactly as the monolith does — no sibling is
    co-mounted under this prefix, unlike auth+gdpr).
  - `_codegen.py` — the `python -m stapel_workspaces._codegen --out docs`
    entrypoint. Explicitly calls
    `stapel_core.django.openapi.swagger._register_jwt_auth_extension()`: the
    monolith's own `urls.py` triggers this drf-spectacular extension
    registration as a *global* side effect of importing `get_dev_urls()`
    (auth's harness gets it for free only because its co-mounted
    `stapel_gdpr.urls` happens to call the same registration); without it,
    protected endpoints would emit without their monolith
    `security: [{"JWTCookieAuth": []}]` entry — a real byte-identity delta,
    not a `$ref` component-closure gap.
- **`docs/schema.json`** (new) — drf-spectacular OpenAPI for workspaces only,
  canonical prefix; **`docs/flows.json`** (new) — empty array, this module has
  no `@flow_step` annotations yet.
- **Byte-identity** with the monolith aggregate's workspaces slice (paths under
  `/workspaces/api/` + their component closure) is **exact**: 8 paths, 13-
  component closure (`WorkspaceResponse`, `MemberResponse`, `StapelError`, …),
  zero diff vs the monolith's committed + freshly-regenerated aggregate.
  `errors.json` re-emission is also byte-identical to the previously-committed
  artifact. No sibling module needed co-mounting for closure (contract-
  pipeline.md §9 Q2): the workspaces slice's `$ref` closure is self-contained
  (workspaces + core's `StapelError`).
- **Gate:** `make contract` / `make contract-check`; `tests/test_contract.py`
  (drift + determinism + canonical-prefix + monolith-slice identity) is the
  CI-enforced gate. The monolith-slice identity test is skipped outside the
  workspace (module CI checks out only this repo).
- No friction from the workspaces brownfield User-model hardcode (already
  resolved upstream, see "fix: workspaces uses AUTH_USER_MODEL not concrete
  User") — the harness mounts cleanly on `AUTH_USER_MODEL="users.User"`,
  same as the existing test conftest.

## 0.4.0 — 2026-07-10

### Added — member listing: `?search=`, `limit`/`offset`, stable display-name sort (BACKLOG G12)

`GET /{workspace_id}/members` now supports server-side filtering and pagination,
so every downstream multi-tenant project with a people-picker stops re-writing
its own member listing (the G12 gap surfaced during a client import).

- **`?search=`** — case-insensitive substring match on the member's email **or**
  display name. Display name resolves the way the surface already presents a
  member (it joins `user`): full name → username → email, via a single
  `Coalesce(NullIf(Trim(Concat(first, last))), username, email)` expression
  reused for both the filter and the sort.
- **`limit` / `offset`** — opt-in pagination window; non-negative ints, junk
  values are ignored (no new 4xx surface). Ordering is **stable** (display name,
  then `id`) so windows never overlap or skip rows.
- **Backward-compatible:** with no query params the full member list is returned
  exactly as before — only now in a deterministic, name-sorted order.
- **OpenAPI contract:** `search`, `limit` and `offset` are declared as
  `OpenApiParameter` (drf-spectacular) on the members-`GET` operation, each with
  a description, so they appear in `docs/schema.json` and the frontend codegen
  sees them — no shadow contract. The monolith aggregate's workspaces slice was
  regenerated in the same change, so the byte-identity gate
  (`test_matches_monolith_workspaces_slice`) stays green.

## 0.3.9 — 2026-07-06

### Changed — admin-suite AS-5: `@access` category rollout

Applies the `stapel_core.access` category decorators (admin-suite §0/AS-5 sweep,
docs/admin-suite.md) to this module's models and swaps the affected `ModelAdmin`
to `stapel_core.django.admin.base.StapelModelAdmin`.

- `WorkspaceInvitation` decorated `@access.secret` (`secret_fields = ("token",)`,
  pinned explicitly even though pattern detection on the field name would also
  catch it) — its bearer invite token is never returned by the invite-creation
  API and is now masked in the admin rather than shown in plaintext to any
  staff with model permissions; only a superuser can view/mutate the row.
  `@access.ops` was considered (the model has the `expires_at`/single-use shape
  the doc calls out) and rejected: `ops`'s admin-layer lockout is total — even a
  superuser cannot add/change/delete — and this repo has no application-level
  revoke endpoint, so `revoked_at` is only ever set via a direct admin edit;
  `ops` would have removed the only working revoke path, `secret` keeps it open
  to superusers.
- `Workspace` and `WorkspaceMember` stay undecorated (implicit
  `@access.standard`) — both are business tables staff work with directly
  (the admin-suite doc's own worked example names `Workspace`).
- Attribute-only change: no migrations
  (`makemigrations workspaces --check --dry-run` reports no changes).

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
