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
| Models (`models.py`) | `Workspace` (UUID pk, `name`, unique `slug`, `type` personal\|work, `owner` FK `PROTECT`, JSON `settings`, `storage_used_bytes` / `storage_limit_bytes` default 5 GiB, soft-delete via `deleted_at`; `db_table workspaces_workspace`), `WorkspaceMember` (unique `(workspace, user)`, `role`, `invited_by`, `invited_at` / `accepted_at` / `last_accessed_at`, `display_name_hint` — a name HINT typed at invite/provision time, copied once at creation, never the canonical name (that lives in stapel-profiles); `db_table workspaces_member`), `WorkspaceInvitation` (email invite, unique single-use `token`, `expires_at`, `accepted_at` / `declined_at` / `revoked_at` — decline ≠ revoke; `display_name_hint` — the invite modal's optional "Имя" field, copied onto the member at accept; derived `status` property `pending|accepted|declined|revoked|expired` (`InvitationStatus`); `db_table workspaces_invitation`). **Lifecycle predicates live here and only here** — `MembershipQuerySet` (`.active()` accepted-and-not-suspended = may act; `.accepted()` suspended included, for surfaces that must report a suspended row and never for authorization; `.suspended(reason=…)`; `.holds_seat()` = billable, deliberately a separate name from `.active()` even while the rows coincide) and `InvitationQuerySet` (`.pending()` live/actionable, `.unresolved()` the clock-free compare-and-set target for accept/decline, `.accepted()`, `.never_accepted()` for GDPR erasure). Nine hand-written copies of "accepted AND not suspended" is what these replace; `tests/test_lifecycle_predicates.py` fails the build if the raw columns are filtered on anywhere outside `models.py`. The rule stops copies from drifting apart — it cannot tell whether the formula matches the business definition. Enums: `WorkspaceType` (`personal`/`work`), `Role` (the BUILTIN four `owner`/`admin`/`member`/`viewer`; the effective role set is extensible via `STAPEL_WORKSPACES["ROLES"]`, role columns are `CharField(32)`) |
| Services (`services.py`) | `create_workspace()` (atomic; seeds OWNER membership, emits `workspace.created`, sends `workspace_member_changed`), `ensure_personal_workspace()` (get-or-create on first login; emits `workspace.personal.created` + `workspace.member_joined`), `resolve_landing_workspace(user, *, origin)` (org-program #85, mandate-model vardict 2026-08-03 — the canon landing-mandate policy: a no-op for `origin="invited"`; for any other origin, reads `STAPEL_WORKSPACES["STREET_LANDING_MODE"]` — `"personal"` default calls `ensure_personal_workspace`, `"none"` returns `None` so the account lands as a guest; an unrecognized mode value fails closed to `None`), `create_invitation()` (token with `INVITATION_TTL_DAYS` lifetime, default 7 days; optional `display_name` hint (the invite modal's "Имя" field) stored as `display_name_hint`, never a canonical name; best-effort `request_notification("workspace.invitation", ...)` via `stapel_core.notifications`), `accept_invitation()` (row-locked single-use token → membership; the invitation's `display_name_hint` is copied onto the membership ONLY on creation — re-accepting an existing membership never clobbers it; re-checks the `workspaces.members.max` entitlement, applies the org's CONFIGURED `provisioned_user_policies` to the joining account via `auth.apply_first_login_policies` inside the same transaction (#90 — fail-closed: an org that stated a precondition does not get a member who skipped it), emits `workspace.member_joined`, invalidates the cross-service membership cache, sends signal), `fetch_profile_display_names()` (best-effort — never an import — the `profiles.display_names` comm Function first, which serves BOTH topologies through one mechanism (in-process in a monolith, the configured route in a split deployment), then the legacy batch read of stapel-profiles' `POST /batch` via the flat-setting `PROFILES_SERVICE_URL`; `{}` when neither is reachable, so `MemberResponse.display_name` falls back to `display_name_hint`), the write half of that same seam (0.21 — over comm, not symbols): `check_display_name(value)` (asks stapel-profiles' `profiles.validate_display_name` what a name may contain and returns ITS `error.400.display_name_*` key or `None`; this module owns no copy of those rules) and `set_profile_display_name(user_id, name)` (calls `profiles.set_display_name`, the named write stapel-profiles publishes from 0.10, which owns the canon, the swap-aware `get_profile_model`, get-or-create and the `profile.changed` emission; returns `None` on success or the error key to answer with). **0.21 deleted the dotted-path seam** (`profiles_in_process` / `_profile_model` / `display_name_canon`): resolving a sibling's symbols through the app registry worked only where profiles was co-mounted, so the roster's name edit answered a permanent 503 in any split deployment — the fleet's only cross-module symbol resolution, and now zero (SWAP003), `decline_invitation()` (row-locked; the invitee's terminal "no", ≠ revoke), `revoke_invitation()` (#109 — the workspace's terminal "no"; same `select_for_update().unresolved()` compare-and-set as accept/decline, emits `workspace.invitation_revoked` with the actor, frees the seat), `resend_invitation()` (#109 — rotates the token, restarts the TTL, re-mails; accepts an EXPIRED invitation on purpose, since a dead TTL is a delivery failure and the three stored terminal stamps are decisions), `issue_invitation_login_grant()` (claim step: `auth.issue_login_grant` comm call with `create_if_missing`; wiring errors propagate — the view answers 503, never allow), `provision_member()` (org-created synthetic user, spec §C1: optional `billing.debit` → `auth.provision_user` with the org's `first_login_policies` **set** (#90, requires stapel-auth >= 0.17) → membership `accepted_at=now, provisioned=True` + `workspace.member_provisioned` emit; credentials letter only when an email anchor was passed), `suspend_member()` / `unsuspend_member()` (spec §C3: not removal — emits `workspace.member_suspended|unsuspended`, cache invalidation, mfa_suspension/mfa_restored letters for the `no_mfa` reason), `enforce_require_mfa()` (sync `auth.mfa_status` sweep, fail-open on auth-down), `lift_no_mfa_suspensions()`, `security_settings_for()` (typed `WorkspaceSecuritySettings` view of `Workspace.settings["security"]`), `reset_member_password()` (#110 — the ORG half of an administrative password reset: which policies, the `workspace.member_password_reset` emit, and telling the member. auth (`auth.admin_reset_password`, >= 0.18) owns the credential half — replace, revoke every live session, raise the first-login demands, write the actor onto its own audit row, refuse a staff/superuser target. The letter deliberately does NOT carry the new password: the admin who ordered the reset hands it over out of band, and a security alert that contains the credential is worth nothing as an alert), `apply_first_login_policies()` (#90 — `auth.apply_first_login_policies` on invitation acceptance; a NO-OP that makes no comm call at all when the org configured no policies, and fail-CLOSED when it did: an acceptance that cannot raise a demanded policy is refused and the whole transaction rolls back) |
| Permissions (`permissions.py`) | `role_at_least()` (rank-based, backward-compatible for the builtin four; `ROLE_HIERARCHY` kept as export), `get_membership()` (`.active()`; `include_suspended=True` widens to `.accepted()` for surfaces that must report the honest `membership_suspended` 403), `require_role()`, `has_capability()` / `require_capability()` (mandate model; suspended memberships never count), `has_active_mandate()` / `is_guest()` (mandate-model vardict 2026-08-03, org-program #85/#87 — the canonical guest predicate: "authenticated, no active mandate anywhere" is a STATE, not a role; workspace-agnostic on purpose, a member of workspace A is still a guest of workspace B) |
| Capabilities (`capabilities.py`, `conf.py`, `checks.py`) | Settings-registry mandate model: `BUILTIN_ROLES` (owner `*` 400 / admin 300 / member 200 / viewer 100), `effective_roles()` (last-wins `STAPEL_WORKSPACES["ROLES"]` overlay; `owner` system-protected), wildcard matcher (`*`, `prefix.*`), `capabilities_for()` / `role_has_capability()`, `role_exceeds_rank(role, actor_role)` (rank-gard, mandate-model vardict 2026-08-03: a role/rank ceiling — "may hand out at most your own rank" — enforced at invite/role-change/provision on top of the capability check; a capability alone only proves "may act at all", not "up to which rank", which stops being safe the moment a below-admin role also carries `members.invite`; unknown ranks on either side fail closed), `BUILTIN_CAPABILITY_LEVELS` + `capability_level()` (step-up levels; the three builtin `high` capabilities — `members.provision`, `members.password.reset`, `workspace.security.manage` — are enforced via `@requires_verification(scope="sensitive")`, and the full effective map is now also served over the wire by `GET roles` as `capability_levels`); system checks E001-E008 validate the overlays |
| Entitlements (`entitlements.py`) | Billing seam: `check_org_entitlement()` / `check_entitlement()` → `billing.check_entitlement` comm Function with degrade-ALLOW when billing is absent (`FunctionNotRegistered` / `FunctionRouteNotConfigured`); enforcement on work-workspace creation (`workspaces.org`), invite/accept (`workspaces.members.max`, seats = `members.holds_seat()` (accepted, non-suspended) + `invitations.pending()` (live: not accepted/declined/revoked, TTL not elapsed) — a suspended membership stopped counting in 0.9.0, a **declined** invitation in 0.10.0; note the count deliberately reserves seats for invitees who have never signed in, which is where it parts ways with the owner's "active user" formula) and member provisioning (`workspaces.provision_user` bool + `debit_provision_credits()` — `billing.debit` with a deterministic `ws-provision:<uuid>` idempotency key when `PROVISION_USER_CREDITS` > 0) |
| HTTP API (`urls.py`, `views.py`) | Workspace list/create (list carries `is_guest` — the wire form of `permissions.is_guest`, computed off the same active-membership query, ANONYMOUS_ALLOWED so it stays the app header's live "which workspaces am I in" path for a guest too), detail (GET/PATCH/DELETE soft-delete), effective role registry (`GET roles` — now also carries `capability_levels`, the effective step-up map, so a frontend `RoleSelect` stops hardcoding its own copy), member list, invite, member role change / removal (last-owner protected; emits `workspace.member_role_changed` / `workspace.member_removed`; invite / role-change / provision all three additionally enforce the rank-gard — `error.403.role_exceeds_inviter_rank`, mandate-model vardict 2026-08-03 — on top of the existing capability + owner-escalation checks), member provisioning (`POST <ws>/members/provision` — HIGH: step-up scope `sensitive` + capability `members.provision` + entitlement/debit; auth's structured errors pass through keyed), administrative password reset (`POST <ws>/members/<uid>/password/reset` — HIGH: step-up `sensitive` + capability `members.password.reset`; only an owner may reset an owner; the target set is *resettable members of this workspace*, and everything outside it — unknown UUID, non-member, member of another workspace, the caller themselves — answers with ONE byte-identical 404, with the capability checked before any target row is read; auth's `error.403.privileged_account` for a staff target passes through keyed; auth unreachable is an honest 503, never a reported success), security-block PATCH (a `settings.security` payload additionally gates on `workspace.security.manage` + step-up and triggers the require_mfa sweep), the admin-side invitation surface (#109: `GET <ws>/invitations` — `?status=pending|never_accepted|all` mapped onto the `InvitationQuerySet` predicates, `?search=` on the email, anchor-paginated on `created_at`, token never in the response; `POST <ws>/invitations/<id>/revoke`; `POST <ws>/invitations/<id>/resend` — all three gated on `members.invite`, invitation id scoped to the workspace so an unknown id and a foreign one answer identically), the roster's two name-edit PATCHes (0.19, absorbed from meettoday: `PATCH <ws>/members/<uid>/name` writes stapel-profiles' `Profile.display_name` — the CANONICAL name, not `display_name_hint`, which goes dark once a profile exists — and `PATCH <ws>/invitations/<id>/name` writes the pending invitation's `display_name_hint`; both gated on `members.role.change` (NOT `members.invite`, deliberately: the hint IS the member's name after acceptance, so a registry that split them would let a role fix a name that reverts), both holding the value to stapel-profiles' `validate_display_name` and surfacing ITS `error.400.display_name_*` keys verbatim, with the 35-char ceiling as the serializer field's `max_length` (`error.400.field.max_length`) rather than a fifth, workspaces-minted key; only an owner may rename an owner; since 0.21 the write travels over comm, so it works wherever profiles is deployed, and a deployment with no provider and no route answers `error.503.profiles_not_configured` (`contact_support` — a configuration fact an operator resolves, distinct from the transient `error.503.profiles_unavailable`), never a 200 over a write that did not happen), the caller's own home workspace (`PUT`/`DELETE me/preferred-workspace` — user-scoped, not workspace-scoped: there is exactly one answer per person. Stored as `WorkspaceMember.is_preferred` with a partial unique constraint "at most one preferred membership per user", echoed on the list response as `preferred_workspace_id` under the same active-membership rule as `default_workspace_id`, and refusing unknown / non-member / pending / suspended targets with ONE identical 404. It is the "explicit choice" `DEFAULT_WORKSPACE_ID` already documents itself as yielding to; clients resolve preferred first, instance default second. Never `last_accessed_at` — that column is telemetry, and reading it as a choice is #239), invitation accept, the public invite-flow surface (`GET invitations/<token>` AllowAny preview with masked email / derived status / `email_registered`, `POST .../decline` authenticated + email-match, `POST .../claim` AllowAny login-grant mint for unregistered emails — throttled via `InvitationThrottle`, Django path-logging suppressed via `TokenPathNoLogMixin` so the URL-borne token never reaches logs), plus internal service-to-service endpoints (`IsServiceRequest \| IsStaffUser`): membership lookup and personal-workspace get-or-create |
| comm Functions (`functions.py`) | `workspaces.check_membership` (constant `CHECK_MEMBERSHIP`; response carries `capabilities` since 0.6) and `workspaces.check_capability` (constant `CHECK_CAPABILITY`), registered idempotently in `AppConfig.ready()` with their schemas |
| Events (`events.py`, `schemas/`) | `EVENT_WORKSPACE_PERSONAL_CREATED = "workspace.personal.created"`, `EVENT_WORKSPACE_MEMBER_REMOVED` / `EVENT_WORKSPACE_MEMBER_ROLE_CHANGED` (member lifecycle, spec §A4), `EVENT_WORKSPACE_MEMBER_PROVISIONED` / `EVENT_WORKSPACE_MEMBER_SUSPENDED` / `EVENT_WORKSPACE_MEMBER_UNSUSPENDED` (security harden, spec §C1/§C3), `EVENT_WORKSPACE_MEMBER_PASSWORD_RESET = "workspace.member_password_reset"` (#110 — the org-side audit record of an administrative reset, carrying the actor and the number of sessions it ended; auth writes its own `AuthAuditLog` row for the same act and the two are meant to agree; NO credential material), `EVENT_WORKSPACE_INVITATION_REVOKED = "workspace.invitation_revoked"` (#109 — the audit answer to "who withdrew that invite", which the row cannot give: it stores `revoked_at` but no `revoked_by`; the invited email is deliberately NOT in the payload), payload dataclasses, `EVENT_REGISTRY`; JSON Schemas in `schemas/emits/`, `schemas/consumes/`, `schemas/functions/` |
| Bus consumer (`management/commands/consume_auth_events.py`) | Listens on `user.registered` (consumer group `workspaces-auth-events`) → `ensure_personal_workspace()` → publishes `workspace.personal.created` |
| GDPR (`gdpr.py`, `apps.py`) | `WorkspacesGDPRProvider` (section `"workspaces"`), registered with `stapel_core.gdpr.gdpr_registry` in `AppConfig.ready()`; export (memberships, owned workspaces, sent invites), delete (memberships removed, every sent invite that never became a membership deleted — `never_accepted()`, so declined/revoked/expired go too, not just live ones — owned workspaces **soft**-deleted), anonymize (`invited_by` cleared on accepted invites) |
| Errors (`errors.py`) | `WORKSPACES_ERRORS` keys (`error.404.workspace_not_found`, `error.403.forbidden_workspace`, `error.403.last_owner_cannot_be_removed`, `error.400.invitation_expired`, `error.403.missing_capability`, `error.402.entitlement_required`, `error.402.member_limit_reached`, `error.400.invitation_declined`, `error.409.email_already_registered`, `error.503.auth_unavailable`, `error.403.membership_suspended`, `error.400.invalid_provision_username`, `error.403.role_exceeds_inviter_rank` (rank-gard, 2026-08-03), `error.503.profiles_unavailable` (0.19), `error.503.profiles_not_configured` (0.21 — the comm route to profiles is not wired; `contact_support`, never `wait_and_retry`), ...) plus the four `error.400.display_name_{too_short,forbidden_chars,invisible_chars,emoji}` keys BORROWED verbatim from stapel-profiles (0.19 — same strings, same English, same remediation, re-declared only so this module's contract is honest about what its name-edit endpoints answer with; the rules behind them stay in that module alone), all registered via `register_service_errors`; `WorkspacesErrorKeysView` |
| Admin (`admin.py`) | `Workspace` / `WorkspaceMember` plain `ModelAdmin`s (business, undecorated); `WorkspaceInvitation` is `@access.secret` and subclasses `StapelModelAdmin` — see "Admin categories" below |
| Public API (`__init__.py`, PEP 562 lazy) | services + comm names + mandate-model helpers (`effective_roles`, `capabilities_for`, `role_has_capability`, `has_capability`, `require_capability`, `is_guest`, `has_active_mandate`, `resolve_landing_workspace`), entitlement seam (`check_org_entitlement`, `EntitlementResult`), event names, `WorkspacesGDPRProvider` — see `__init__.py` `_EXPORTS` |

Consumer-side helpers live in **stapel-core**, not here: `stapel_core.django.workspaces`
(`get_membership`, `require_role`, `invalidate_membership_cache`,
`get_or_create_personal_workspace` — HTTP against the internal API with a 30 s cache) and
`stapel_core.comm.call("workspaces.check_membership", ...)`. Other modules use those;
they never import `stapel_workspaces`.

## Extension points (fork-free)

### Settings

`conf.py` exposes the `STAPEL_WORKSPACES` namespace (`workspaces_settings`,
`stapel_core.conf.AppSettings`; resolution dict → env → default):

| Key | Kind | Default | What it customizes |
|---|---|---|---|
| `STAPEL_WORKSPACES["ROLES"]` | merge-registry (last-wins per role key) | `{}` | Product roles overlaid over `capabilities.BUILTIN_ROLES`; an entry replaces the builtin entry whole (`{"rank": int, "capabilities": [str]}`); `owner` is system-protected (checks E002) |
| `STAPEL_WORKSPACES["CAPABILITY_LEVELS"]` | merge-registry | `{}` | Step-up levels (`"standard"`/`"high"`) overlaid over `BUILTIN_CAPABILITY_LEVELS`; the builtin `high` pair is enforced by `@requires_verification(scope="sensitive")` on provision + security PATCH |
| `STAPEL_WORKSPACES["INVITATION_TTL_DAYS"]` | tuning | `7` | Invitation link lifetime (`services.create_invitation`) |
| `STAPEL_WORKSPACES["INVITATION_THROTTLE"]` | tuning | `"30/min"` | DRF `ScopedRateThrottle` rate (scope `workspace-invitation`) for the AllowAny invite endpoints (preview + claim) — enumeration backstop; `None` disables |
| `STAPEL_WORKSPACES["PROVISION_USER_CREDITS"]` | tuning | `0` | Credits debited per provisioned org user via `billing.debit` (`0` = free; degrade-allow without billing) |
| `STAPEL_WORKSPACES["STREET_LANDING_MODE"]` | axis (`"personal"` \| `"none"`) | `"personal"` | Read by `services.resolve_landing_workspace(user, origin=...)` for every un-invited (`"street"`/`"anon"`) origin (org-program #85, mandate-model vardict 2026-08-03): `"personal"` (default, back-compat) mints/get-or-creates a Personal workspace and makes the user its OWNER — the pre-#85 behavior, unchanged for any deployment that never touches this key; `"none"` creates no workspace at all, so the account is a guest (`permissions.is_guest`) until an existing organization invites it. Never consulted for `origin="invited"` — an accepted invitation grants a mandate through `accept_invitation` regardless of this setting. |
| `FRONTEND_URL` | flat Django setting | `""` | Base URL for the frontend links in letters: `/invite/{token}` (invite), `/login` (provisioned account), `/settings/security` (mfa_suspension), `/workspaces/{slug}` (mfa_restored) |
| `STAPEL_COMM` / `STAPEL_BUS_BACKEND` | core namespaces (`stapel_core.comm.config`, `stapel_core.bus`) | — | Transport for all emits/consumes/function calls (in-process in a monolith, bus in microservices) — deployment config, not code |
| `WORKSPACES_SERVICE_URL`, `SERVICE_API_KEY` | env vars (consumer side, in `stapel_core.django.workspaces`) | `http://stapel-workspaces:8000`, `""` | Where other services reach the internal membership API, and the `X-API-KEY` they present |

**Not configurable today** (hard-coded; making any of them a setting is an upstream
contribution): default storage quota (5 GiB, `Workspace.storage_limit_bytes`), slug
auto-generation (`services._make_unique_slug`), the 30 s consumer-side membership
cache TTL (`stapel_core.django.workspaces.CACHE_TTL_SECONDS`).

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

Since 0.6 the role set is a **settings-registry** (org-program spec §A1):
`capabilities.BUILTIN_ROLES` ships the four builtin roles (owner rank 400 `*`,
admin 300, member 200, viewer 100) and a host project adds or overrides roles via
`STAPEL_WORKSPACES["ROLES"]` (last-wins per key, entry replaces whole; capability
strings are namespaced `"<domain>.<action>"`, wildcards `"*"` / `"prefix.*"`).
`owner` cannot be overridden or removed (system check E002 + runtime backstop);
per-workspace custom roles are deliberately NOT supported (roles are product-level;
the `effective_roles()` seam allows a future per-workspace overlay without API
breakage). `models.Role` (TextChoices) stays as the builtin enum; serializers
validate against the effective registry (the stapel-recordings `SourceType`
precedent). The `GET /workspaces/api/v1/roles` endpoint and `my_capabilities` on
workspace responses surface the registry to frontends.

What **is** app-layer:

- Capability checks in your own code via `stapel_workspaces.permissions.has_capability`
  / `require_capability` (in-service), `stapel_core.django.workspaces.require_capability`
  or `comm.call("workspaces.check_capability")` (from any other service, no import of
  this app); role-threshold checks (`role_at_least` / `require_role`) remain for
  rank-ordered decisions.
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
| `MemberListView` | `<ws>/members` (`workspace-members`) | — | `MemberResponseSerializer` (anchor-paginated: `?search=`, `anchor`/`limit`/`direction`) |
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
| `workspace.member_provisioned` | `workspace_id`, `user_id`, `role`, `provisioned_by` | Org-created (synthetic) member joined via `POST members/provision` — audit/metering; never carries credentials |
| `workspace.member_suspended` | `workspace_id`, `user_id`, `role`, `reason` | Membership suspended (spec §C3; canonical reason `no_mfa`) — subscribers revoke live access like on `member_removed` |
| `workspace.member_unsuspended` | `workspace_id`, `user_id`, `role`, `reason` | Suspension lifted (member enabled MFA, or the org dropped `require_mfa`) |

**Consumes** (`actions.py`, `@on_action`; handlers must be idempotent — delivery is
at-least-once):

| Event | Handler | Effect |
|---|---|---|
| `user.deleted` | `handle_user_deleted` | `WorkspacesGDPRProvider().delete(user_id)` — memberships removed, owned workspaces soft-deleted |
| `user.mfa_disabled` | `handle_user_mfa_disabled` | Suspend the user's memberships (reason `no_mfa`) in every workspace whose `settings.security.require_mfa` is on |
| `user.mfa_enabled` | `handle_user_mfa_enabled` | Lift the user's `no_mfa` suspensions (only that reason) |
| `user.deactivated` | `handle_user_deactivated` | The ACCOUNT was administratively deactivated in auth (#92) — suspend **every** accepted membership (reason `account_deactivated`), whatever the workspace's policy says. Reversible; no row deleted; the seat is freed |
| `user.reactivated` | `handle_user_reactivated` | Lift the `account_deactivated` suspensions only — a `no_mfa` suspension belongs to the MFA consumer and stays |

`user.deactivated` and `user.deleted` are deliberately **different paths**:
deactivation is administrative and reversible and must leave a *suspended*
membership to come back to; GDPR deletion removes the rows. See
`models.MemberState` for the full `invited`/`active`/`suspended`/`deleted`
vocabulary.

(`schemas/consumes/user.deletion_initiated.json` is declared, but `actions.py` currently
subscribes only to `user.deleted`.)

Additionally, the `consume_auth_events` management command (bus deployments) consumes
`user.registered` and bootstraps the personal workspace.

**Functions provided:**

| Function | Payload | Returns | Notes |
|---|---|---|---|
| `workspaces.check_membership` (`CHECK_MEMBERSHIP`) | `{"workspace_id": uuid-str, "user_id": uuid-str}` | `{"is_member": bool, "role": str \| null}` | Only *accepted*, *non-suspended* memberships count. Mirrors the internal HTTP endpoint (`InternalMembershipView`). Call via `stapel_core.comm.call` — never import this app |

**Functions called** (name-addressed, `stapel_core.comm.call`; the provider's module is never imported):

| Function | Where | Notes |
|---|---|---|
| `profiles.set_display_name` | `services.set_profile_display_name` (0.21) | The roster's name edit. Authority is decided here — `members.role.change`, plus "only an owner renames an owner" — and the write is performed by the module that owns the data, same split as `billing.debit`. Floor: **stapel-profiles >= 0.10**. Neither a provider nor a route → `error.503.profiles_not_configured` (`contact_support`) and a startup warning (`stapel_workspaces.W001`); a call that was made and failed → `error.503.profiles_unavailable` (`wait_and_retry`) |
| `profiles.validate_display_name` | `serializers.DisplayNameUpdateRequestSerializer` via `services.check_display_name` (0.21) | The name canon, asked rather than copied. Best-effort: no provider means no canon applied here — never a locally invented rule |
| `profiles.display_names` | `services._fetch_profile_display_names` (0.21) | Roster names, one mechanism for both topologies; falls back to the `PROFILES_SERVICE_URL` HTTP batch |
| `billing.check_entitlement` / `billing.debit` | `entitlements.py` | Degrade-ALLOW when billing is absent |
| `auth.provision_user` / `auth.admin_reset_password` / `auth.apply_first_login_policies` / `auth.mfa_status` / `auth.issue_login_grant` | `services.py` | See the Services row |

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

### Contract emission — the `schema` + `flows` + `errors` triad

This module emits its **own** machine-readable API contract, per-module, so the
frontend codegen can read a committed, version-pinned artifact instead of
checking out the monolith aggregate at floating `main` (contract-pipeline.md
§2, verdict **A**: contract = a reviewable commit, like `docs/errors.json`
always was). Copied from stapel-auth's reference implementation. The triad
lives in `docs/`:

```
docs/schema.json   drf-spectacular OpenAPI, this module only, canonical /workspaces/api/ prefix
docs/flows.json    generate_flow_docs machine artifact — empty array (no @flow_step yet)
docs/errors.json   generate_error_keys registry (the original per-module etalon)
```

The emitted `schema.json` is **byte-identical to the monolith aggregate's
workspaces slice** — the 8 paths under `/workspaces/api/` plus their transitive
`$ref` component closure (self-contained: workspaces' own response/request
schemas + core's `StapelError`; no sibling module needed for closure, unlike
auth↔gdpr). `tests/test_contract.py::test_matches_monolith_workspaces_slice`
asserts it in the workspace (skipped in module CI, where the monolith isn't
checked out).

**Harness** (three small files, plus the shared mechanism in
`stapel_tools.codegen`):
- `_codegen_settings.py` — the single `settings.configure(**kwargs)` block,
  shared with `conftest.py` so the test instance and the codegen instance can
  never drift. `contract=True` swaps in the production `REST_FRAMEWORK` (DRF
  caches it on first access, so it must be right at configure time).
- `codegen_urls.py` — mounts `stapel_workspaces.urls` alone at the canonical
  `workspaces/api/` prefix, exactly as the monolith does (no sibling co-mount:
  workspaces is the only module under this prefix in
  `stapel-example-monolith/svc-app/core/urls.py`).
- `_codegen.py` — configures the instance on `codegen_urls`, forces
  `spectacular_settings.SCHEMA_PATH_PREFIX = "/"` (drf-spectacular derives
  operationIds from the common path prefix of all endpoints — `/` in the
  multi-module monolith, so operationIds keep the mount segment,
  `workspaces_api_*`), and **explicitly calls**
  `stapel_core.django.openapi.swagger._register_jwt_auth_extension()`. The
  monolith's own urls.py triggers this drf-spectacular security-scheme
  registration as a side effect of importing `get_dev_urls()`; auth's harness
  gets it for free only because its co-mounted `stapel_gdpr.urls` happens to
  call the same registration — workspaces has no such sibling, so the harness
  registers it directly. Without this, protected endpoints would emit without
  their monolith `security: [{"JWTCookieAuth": []}]` entry (a real
  byte-identity delta, not a `$ref` closure gap).

**Gate:** `make contract` re-emits; `make contract-check` regenerates into a
temp dir and diffs — identical discipline to `test_error_keys`. The
CI-enforced gate is `tests/test_contract.py` (pytest, run in the module's
venv). Regenerate after any serializer/view/url/error change:

    make contract        # or: python -m stapel_workspaces._codegen --out docs

then commit `docs/{schema,flows,errors}.json`.

### Admin categories (`stapel_core.access`, admin-suite AS-5)

`Workspace` and `WorkspaceMember` are business tables and stay undecorated (implicit
`@access.standard` — the doc's own worked example literally names `Workspace`, and
`WorkspaceMember` is the core membership/role table staff manage directly).

`WorkspaceInvitation` is decorated `@access.secret` and its `ModelAdmin` subclasses
`stapel_core.django.admin.base.StapelModelAdmin` (`secret_fields = ("token",)`,
pinned explicitly though pattern detection on the field name would also catch it).
Its `token` (`secrets.token_urlsafe(32)`, unique, single-use) is a bearer capability
— the invite-accept API endpoint deliberately never returns it in the response DTO,
only the notification email carries it — so plaintext exposure in the admin is the
same class of risk as `ScopeToken` (`stapel-core/django/gateway/models.py`), the
reference `@access.secret` precedent with the same `token` + `expires_at` +
`revoked_at` shape. `@access.ops` was considered and rejected: the admin layer's
read-only lockout for `ops` applies even to a superuser (`StapelModelAdmin.
has_add_permission` et al. hard-`return False`), and there is no application-level
revoke endpoint in this repo today — `revoked_at` is only ever set via a direct
admin edit (or GDPR bulk-delete of unaccepted invites) — so `ops` would remove the
only working revoke path. `@access.secret` keeps that path open to superusers while
masking the token from any lower-privileged staff view. Attribute-only change: no
migrations (`makemigrations workspaces --check --dry-run` reports no changes).

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
- **Don't spell the lifecycle columns by hand.** "Accepted and not suspended"
  (and the four-timestamp invitation state) belong to `MembershipQuerySet` /
  `InvitationQuerySet` in `models.py`; `tests/test_lifecycle_predicates.py`
  fails the build on a raw `accepted_at__isnull` / `suspended_at__isnull` /
  `declined_at__isnull` / `revoked_at__isnull` in a query anywhere else. The
  nine hand-written copies this replaced disagreed with each other and one of
  them billed organizations for suspended members. If a place genuinely needs
  a *different* answer, add a named predicate and say what it means — do not
  re-copy the columns.

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
notifications stack, not here), transport (`STAPEL_COMM`).

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
