# Changelog

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
