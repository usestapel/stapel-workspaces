# Changelog

## [Unreleased]

## [0.28.0] — 2026-08-18

### Added — a refusal now answers both sides

Declining sent nothing at all. The person who refused got no receipt for a
decision they cannot undo, and the inviter kept waiting for an answer already
given. Both letters go out through the two notification types
stapel-notifications 0.12.0 added: a receipt to the invited ADDRESS (never to
a session — the invitee usually has no account, which is why decline rests on
the token), and an answer to the inviter naming the address THEY typed and
nothing else about whoever declined.

Each letter is requested inside its own guard: a failing receipt cannot cost
the inviter their answer, and neither can cost the invitee their `204`. The
refusal is complete the moment the row is written; nothing about the mail may
reach back into it.

### Added — the guarantee that went missing with the auth check

When decline moved to `AllowAny` in 0.27.0, `test_requires_auth` was deleted
and nothing replaced it, leaving no assertion that the token is REQUIRED. An
unknown token is refused, and a revoked invitation cannot be declined.

## [0.27.0] — 2026-08-17

### Changed — declining an invitation no longer requires creating the account being declined

`POST /invitations/{token}/decline` was `IsAuthenticated` + email-match. The
invitee who has no account here is the majority case on that path — it is the
entire reason `claim` exists — so saying "no thanks" meant first registering
the very account the person was refusing to create. The flow asked for the
commitment before the decision, and the refusal left a live, empty account
behind it. Verified on the stand.

The endpoint is now `AllowAny` on the invite token, like its siblings
`GET /invitations/{token}` (preview) and `POST /invitations/{token}/claim`,
with the same throttle and the same token-never-logged discipline.

**No new capability is handed to a token holder.** `claim` already takes this
same token from an anonymous caller and mints a *login grant for the invited
mailbox* — strictly more than declining. What the token still cannot do is act
as somebody else: a request that arrives with a session naming an address must
be the invited address, so a signed-in person cannot resolve an invitation that
is not theirs. That check is unchanged, and a test pins it.

A session that names no address at all — a guest session, which this product
mints at meeting doors — is now treated as the anonymous case rather than as a
mismatched account. Previously `ANONYMOUS_DENIED` refused it outright, so a
guest who was also an invitee could not decline at all while a browser with no
session could.

`services.decline_invitation(user=…)` is now optional and defaults to `None`.
The audit line is written either way: with no actor to name, `subject_email`
still carries the invited address, which is the fact worth keeping.

## [0.26.0] — 2026-08-16

### Added — `workspaces.check_mandate`: does this user hold a mandate anywhere

`has_active_mandate` / `is_guest` have been the canonical guest predicate since
the mandate-model verdict, and had zero consumers outside this package — a
sibling service that does not embed this app could not reach them. The comm
surface published `check_membership` and `check_capability`, both
workspace-scoped; neither answers the workspace-AGNOSTIC question.

- `functions.check_mandate` (+ `schemas/functions/workspaces.check_mandate.json`)
  answers the contract stapel-core 0.27.0 declares, so the split deployment gets
  the same verdict the monolith reads in-process.
- `services.has_active_mandate_for_id` — the same predicate addressed by id, the
  form the provider is handed, so the wire answer and the local one cannot drift
  into two definitions of "active".

Authorization is unchanged: this reads existing rows and creates none.

### Added — deleting a workspace is a transition, not a column write

`DELETE <workspace>` already existed and already set `deleted_at`. Everything
around it did not: no peer was told, the workspace's own audit journal never
recorded its end, the transition lived in the view against this module's own
"one write path" rule, and the single refusal (`403` for a non-owner) was the
only one there was — nothing stopped an owner deleting the instance's
**default** workspace, whose OWNERs are `services.instance_owner_ids()`.
Deleting it empties the instance's owner set, and under the `instance_owner`
create policy nobody could ever found a workspace again.

- `services.delete_workspace()` — the one write path: terminal `deleted_at`,
  the `workspace.deleted` event, and the audit line, in one transaction under
  `lock_workspace`. A terminal state rather than row removal: peers key their
  data by this id and this module has no authority over their tables, so the
  row stays resolvable and the event carries the news. Announce, never cascade
  — how long a recording outlives its workspace is the recordings module's
  policy.
- `events.EVENT_WORKSPACE_DELETED` (+ `schemas/emits/workspace.deleted.json`),
  and `AuditAction.DELETED`. Ids and counts only, never emails.
- Two refusals that name their cause, instead of a bare "no":
  `error.409.workspace_is_instance_default` and
  `error.409.workspace_is_personal` (the latter only where
  `STREET_LANDING_MODE="personal"` would re-mint it, so the stated reason is
  never false).
- `WorkspaceResponse.can_delete` / `delete_blocked_reason` — the verdict and
  its code on the detail response, from the same `deletion_block_reason()` the
  DELETE raises from, so a screen cannot draw a control the endpoint refuses.
- `e2e/` — a real host (auth + workspaces over HTTP) driving the lifecycle
  including every refusal. Not shipped in the wheel.

Deleting a workspace is **not** a GDPR act: a workspace is an organizational
object, and the natural-person axis stays with `WorkspacesGDPRProvider`.

### Changed — `stapel-core` floor raised to 0.27.0

`django/mandate.py` is the seam `workspaces.check_mandate` answers through, and
it exists only in 0.27.0. Core also OWNS `error.503.mandate_unavailable` there
and ships it in every language, so this module's duplicate ru/es entries are
deleted — the loader merges the owner's catalog, and two copies of one string
is how the two drift apart.

## [0.25.2] — 2026-08-15

### Changed — `stapel-core` floor raised to 0.26.0

`docs/errors.json` carries an `owner` per entry, and only stapel-core 0.26.0
emits it. The floor stayed at `>=0.24.0`, so a consumer resolving an older
core regenerated an artifact without `owner` and the drift gate went red —
the field was declared but never required. The floor now matches the
artifact that is committed.

## [0.25.1] — 2026-08-15

### Fixed — `E011` asked the route table a question only http can answer

`stapel_workspaces.E011` (new in 0.25.0) decided "nothing is wired" by
looking for a `billing.` prefix in `STAPEL_COMM["FUNCTION_ROUTES"]`. That
table exists **only for the http transport**: under
`FUNCTION_TRANSPORT="nats"` the subject IS the function name and no routes
are configured at all (`stapel_core/comm/nats.py`), so a fleet correctly
serving billing through `manage.py serve_functions` got E011 and
`manage.py check` refused the boot. The seam worked; the check read the
wrong evidence.

Reachability is now asked per transport, the way `comm.call()` dispatches:

| `FUNCTION_TRANSPORT` | wired means |
| --- | --- |
| `inprocess` | a provider for `billing.check_entitlement` is registered in this process |
| `http` | a `FUNCTION_ROUTES` prefix matches the function name |
| `nats` | nothing to wire — no route table exists |
| dotted path | the custom transport does its own addressing |
| anything else | `call()` cannot dispatch it, so the seam is unreachable |

Two consequences beyond the NATS unblock, both deliberate. A process that
runs `FUNCTION_TRANSPORT="http"` **and** installs `stapel_billing` still
needs a route: `call()` never consults the registry under http, so the old
`apps.is_installed()` short-circuit was silent about a deployment whose
every entitlement call would 503. And an `inprocess` deployment that
registers the provider without the app (a host publishing its own
`billing.check_entitlement`) is now correctly seen as wired.

`stapel_workspaces.W001` (the `profiles.set_display_name` write) read the
same evidence the same way and is fixed with the same helper — it warned
about a healthy NATS deployment.

No behavior changed in the seam itself: `check_entitlement` still fails
closed, and `ALLOW_UNBILLED` is still the one way to declare an instance
that sells nothing.

## [0.25.0] — 2026-08-14

### Changed — BREAKING: the billing seam fails closed

**Read this before upgrading if you run stapel-workspaces without
stapel-billing.** The entitlement seam used to treat "nothing answered
`billing.check_entitlement`" as ALLOW, on the reading that an unbilled
deployment is unrestricted by construction. The seam cannot tell that
situation apart from a billing service that crashed, scaled to zero, lost
its `FUNCTION_ROUTES` entry or sits behind a gateway rendering JSON 404s —
on NATS a `NoRespondersError` arrives as the very same
`FunctionNotRegistered`. The price of one outage was therefore every seat
ceiling, every org gate and every per-user debit at once, announced only by
a `logger.debug`.

Those two comm wiring errors now raise `BillingUnavailable`, which the API
answers with **503 `error.503.billing_unavailable`** (not 402: nobody yet
knows whether the plan allowed it, so this is a server fault to page on,
not an upsell to show a customer). `billing.debit` closes the same way —
provisioning paid capacity and charging nobody was the same finding as
letting the check through.

**Upgrade note.** A deployment that genuinely sells nothing declares it
once: `STAPEL_WORKSPACES["ALLOW_UNBILLED"] = True`, which restores the old
allow for the wiring errors only. You will not discover this in
production — the new `stapel_workspaces.E011` system check fails
`manage.py check` at boot when plan ceilings are enforced and neither
`stapel_billing` is installed nor a `billing.` prefix is routed. The key
is `no_env`: set it in `STAPEL_WORKSPACES`, not in the environment, so that
no stray same-named variable in a shared pod can open the paywall. (It also
goes through the same `_TRUTHY` coercion as its neighbours, so it cannot
read `"false"` as True the way `bool("false")` would.)

### Fixed — the three HIGH findings of the 2026-08-11 security audit

**WORK-01 — `require_mfa` said "done" for work it had not done.** Turning
the policy on saved the flag, ran a sweep whose Boolean result the view
threw away, and answered 200; the sweep stopped at the first auth error, so
an organization could be told MFA was required with none of its members
checked, and anyone joining afterwards walked in unexamined. The policy now
has state (`WorkspaceMFAEnforcement`: pending → enforcing → enforced/failed,
with attempts, coverage and the last auth error) and per-member evidence
(`WorkspaceMember.mfa_compliant`, where NULL means "nobody has asked" — the
value the old code could not express). Enforcement moved to the door:
`permissions.get_membership` — and with it `workspaces.check_membership`
and the internal membership endpoint — refuses a member whose factor is
unconfirmed while the policy is on, verifying on the spot and storing the
answer. `retry_mfa_enforcement` + `manage.py enforce_workspace_mfa` are the
durable half; `mfa_enforcement` on the workspace detail/PATCH response and
`mfa_compliant` on the roster are the visible one. An auth outage still
suspends nobody — it simply stops admitting the unverified.

**WORK-02 — owner, seat and invitation invariants raced.** Each was decided
on a snapshot and enforced afterwards: two demotions each saw "another
owner exists" and left none, two invite batches each saw the last free
seat, provisioning counted no seats at all, and two live invitations for
one address meant two working tokens and two billed seats. Every mutating
path now takes the workspace row lock first (`services.lock_workspace`) and
re-reads what it decides from inside it — `change_member_role` /
`remove_member` re-check the surviving owner, `invite_members` counts and
writes the batch in one transaction, accept/resend/provision reserve their
seat there. The database states the one invariant it can: a partial unique
constraint on (workspace, email) for unresolved invitations, with the
re-invite refreshing the existing row (migration `0010` revokes
pre-existing duplicates first).

**WORK-03 — provisioning and login grants leaked control.** Provisioning is
a saga on `WorkspaceProvisionOperation` with a stable operation id: a
completed operation replays into the member it already made, a resumed one
keeps the account auth minted, and a failure compensates — refund
attempted, and what billing cannot take stays as `compensating` for
`manage.py reconcile_provisioning`. The claim endpoint mints ONE live login
grant per invitation (`INVITATION_LOGIN_GRANT_TTL_SECONDS`, claimed by a
conditional UPDATE), so a leaked invite link is no longer an endless supply
of sign-in credentials; a second claim answers
`error.429.invitation_grant_pending` with `Retry-After`. The
provisioned-account letter no longer carries the generated password unless
`PROVISION_EMAIL_INITIAL_PASSWORD` is set.

Known limits, recorded rather than papered over: real parallel execution of
the WORK-02 races needs PostgreSQL (SQLite ignores `SELECT ... FOR UPDATE`,
so the tests pin the decision-under-lock and the one-transaction
reservation); binding a login grant to workspace/purpose/TTL and replacing
the provisioned password with a one-time activation link both need
stapel-auth seams that do not exist yet; and `billing.credit` is not
published by stapel-billing, so a refund usually cannot be made from here —
which is why the debt is a queue instead of a log line.

### Changed — the membership journal moves into the core event store; the bespoke table is gone

0.24.0 built `WorkspaceAuditEvent` — an append-only table with its own
pagination, no retention and no aggregation — one floor above
`stapel_core.eventstore`, the platform's append-only stream primitive that
already had cursor reads, retention, rollups and a pluggable backend, and
that the privilege gateway's audit already writes to. That is this fleet's
most-repeated defect shape (a mechanism built in core, a consumer that never
picked it up), and this release deletes the instance of it:

- Lines are written to the event-store stream
  `STAPEL_WORKSPACES["AUDIT_STREAM"]` (default `workspace.audit`) through a
  new deployment-owned sink seam `STAPEL_WORKSPACES["AUDIT_SINK"]` — the
  same callable contract as `STAPEL_GATEWAY["AUDIT_SINK"]`, so one custom
  sink serves both. The vocabulary (`models.AuditAction`), the write
  discipline (`audit.record_audit`, still the one write path, still
  never-raising) and the read mandate (`members.view`) stay in this module;
  the storage stops being its business.
- **`GET <workspace_id>/audit` does not change shape.** Same envelope, same
  ISO-timestamp anchors, same item fields and ids — the read path goes
  through the store's new anchor adapter, which speaks the released
  `AnchorPagination` wire contract (pinned by
  `tests/test_audit.py::test_the_released_anchor_envelope_survives_the_storage_change`).
  One deliberate edge change: a malformed `anchor` now answers an empty page
  (the malformed-`user_id` rule) where 0.24 answered a 500.
- Migration `0009` is deletion-driven WITH the data path: it replays every
  `workspaces_audit_event` row into the configured store (original
  timestamps become the event `ts`, original UUID ids stay the line's `id`,
  so pre-migration pages read back identically), then drops the table.
  Deployments need `stapel_core.django.eventstore` in `INSTALLED_APPS`
  (already true of any deployment on core's `COMMON_INSTALLED_APPS`).
- Retention, rollups and backend routing arrive for free via
  `STAPEL_EVENTSTORE` (`RETENTION["workspace.audit"]`, `ROUTES`), and the
  journal shows up in core's cross-module `manage.py audit_trail`.

Requires stapel-core with the event-store journal reads (`query(reverse=)`,
`eventstore.anchor`) — the same branch, unreleased.

## [0.24.1] — 2026-08-13

### Fixed — the audit contract advertised a shape the endpoint never sends

`WorkspaceAuditView` paginates (`get_paginated_response`) but did not declare
`pagination_class`, which is what drf-spectacular reads to wrap the response
schema. So `docs/schema.json` promised a bare `AuditEventResponse[]` while the
endpoint returned `{items, has_next, next_anchor}` — and a client generated
from that contract would have been typed against a shape the server never
sends. Caught by the frontend pair one hour after 0.24.0, before any consumer
shipped against it.

The declaration is the fix, not a schema override: the neighbouring member and
invitation lists have always declared theirs the same way.

## [0.24.0] — 2026-08-13

Three things a multi-workspace product could not do, all library-side: the
products consuming this had no seam through which they could have done them.

### Added — a workspace keeps its membership history

"The admin section has no audit of workspace members" was literally true:
there was no record to read. The comm events this module emits are
fire-and-forget notifications to other services — nothing keeps them — and half
the transitions that matter emit nothing at all: an invitation created, an
invitation accepted, an account born from one.

`WorkspaceAuditEvent` is append-only by construction (no update path, no delete
path, no editable surface — a history somebody can edit answers a different
question from the one it is asked). `actor` and `subject` are separate columns
because "Anna removed Boris" and "Boris left" are the same row with the two
swapped; a suspension applied by a policy sweep has no actor, and naming one
would be an invention.

Eleven actions, a closed vocabulary. Each is written from the SERVICE that owns
its transition, never from a view — the same rule the emits already follow, for
the same reason: a second door comes with a second chance to forget.

`GET <workspace_id>/audit` is gated on `members.view` rather than a mandate of
its own: every role that may see who is in the room may see how they got there,
and a separate capability would let a deployment grant one without the other,
which describes no real product. A malformed `?user_id=` matches NOBODY rather
than being ignored — handing back the whole history under a request that asked
for one person's is the loudest possible wrong answer.

`test_every_emitted_membership_event_has_an_audit_action` matches emitted event
names against the action vocabulary, so a future transition cannot ship
emitting-but-not-recording — this module's recurring failure mode. A failing
audit never fails the change it records, verified by breaking the write on
purpose.

Migration `0008_audit_event` (additive: one new table, no column touched).

### Added — a private cloud decides who may found a workspace

`POST /workspaces` asked one question: are you a real account? On a public
cloud that is the right answer. On a private one it is not — an instance where
entry is by invitation only, and where the operator provisions the space, still
let every invited member mint their own organization beside it.

`WORKSPACE_CREATE_POLICY`: `open` | `instance_owner` | `closed`. Empty (the
default) DERIVES it from `STREET_LANDING_MODE` — `personal` is a public cloud
and answers `open`, anything else answers `instance_owner` — because the two
axes answer the same product question, and a deployment that closed its landing
mode while leaving creation open would carry that gap unnoticed. An explicit
value always wins. The instance owner is the OWNER of `DEFAULT_WORKSPACE_ID`:
no second authority to keep in sync with the first.

`services.can_create_workspace` answers both the gate and the new
`can_create_workspace` flag on the workspace-list response, so a drawn
"+ New space" button and the door it opens cannot disagree. The flag is the
ANSWER for the caller, not the policy name — resolving `instance_owner`
client-side would put the owner lookup in every client.

Two boot checks: **E010** for a misspelled policy, which resolves
RESTRICTIVELY rather than to `open` (degrading a typo into an open cloud is the
failure this key exists to prevent), and **W002** for `instance_owner` with no
default workspace, where nobody can create anything and the deployment should
hear it at boot rather than at the first 403.

New error key `error.403.workspace_creation_closed` (+ru/es) — its own key
rather than `forbidden_workspace`: that one means "you are not in THAT space",
this one is about a space that does not exist yet, and they lead to opposite
screens.

### Added — a workspace says who owns it

A workspace NAME stopped identifying a workspace: a person can belong to
several spaces all called "Personal" — one per company that made them one — and
ownership can be handed over, so "Personal" answers nothing in a picker. The
owner does.

`owner_display_name` on `WorkspaceResponse`, resolved through the same
best-effort profiles batch `MemberResponse.display_name` already uses: one call
for the whole list, empty rather than invented when the owner has no profile,
no route, or profiles is not installed. Resolved here rather than in each
client — two products draw this caption and one of them does not even carry
`@stapel/profiles-react`; one line of text is not worth a new frontend
dependency and an N+1 per picker. The single-workspace path resolves it too, so
the field cannot be populated on the list and empty on the detail of the same
workspace.

## [0.23.0] — 2026-08-10

Three defects on the workspace-invitation path, all found by review of a live
product, all library-side: the product consuming this has no code on this path
and no seam through which it could have fixed any of them.

### Fixed — this module translates only the keys it owns

`translations/errors.{ru,es}.json` each carried 41 verbatim copies of the
cross-cutting keys stapel-core owns. None was an intentional reword: before
stapel-core 0.22.0 the coverage gate took its canon from the whole in-process
registry, so going green *required* copying them. Core ships those catalogs
itself now and the loader merges them, so the copies were a second, drifting
shadow of texts this module does not answer for — and `test_catalog_gate_green`
went red on them, which is what held this release (and every other tag in the
fleet) back. ru and es go 68 → 27 keys.

The reference does not move: `docs/errors.{en,ru,es}.md` regenerated after the
deletion are **byte-identical** to the ones regenerated before it, because
stapel-core 0.23.1 resolves a key this module does not own from its owner's
catalog (`module_catalog`). Without that fix the prune would have downgraded 41
Russian rows to `_(en)_` English fallbacks with nothing saying so;
`test_error_reference_matches_a_fresh_regeneration` now keeps the committed
reference and a fresh regeneration byte-equal, so it can never again be green
while being unreproducible.

The `stapel-core` pin moves to `>=0.23.1`: with an older core these pruned
catalogs resolve to English at runtime.

### Fixed — the invitation letter for someone with no account was written, shipped, and never sent

`workspace.invitation.new_user` has existed in stapel-notifications since
0.6.1 with its own routing entry, its own template and six translation keys.
Its copy says the button below **creates your account** and joins you to the
workspace — the only honest sentence for a stranger, and a lie to somebody who
already has a login. Nothing in this module ever selected it. Every invitation
went out as the base type, so for two minor versions a person with no account
received a letter written for someone who had one.

`_send_invitation_notification` now chooses between the two on the value it was
already computing one line above the send (`invitee`, fetched to attach
`user_id`). Nothing else about the two requests differs: same variables, same
recipient address, and the pre-existing `user_id`-when-known targeting.

**This is not an account-enumeration oracle**, and that was checked against the
code rather than assumed: `create_invitation` has no duplicate/exists branch,
the 201 body is the same DTO either way, no event is emitted, neither branch
logs, and the only difference an outsider could observe is which of two letters
lands in the invited address's **own** mailbox. Account existence remains
answerable from outside only where this module deliberately answers it —
`email_registered` on the invitation preview, which is token-gated,
email-masked and throttled.

### Added — a gate on the whole class, not only this instance

The defect is the fleet's most-repeated shape: a mechanism was built, its
consumer never picked it up, and nothing noticed, because every test asserted
what the code does rather than what the catalog offers.

`tests/test_invitation_letter.py::TestEveryWorkspaceLetterIsReachable` reads
the notification catalog stapel-notifications actually ships, extracts the
notification types this package can actually request (statically, from the
AST — resolving literals, module constants, conditional expressions and the
`notification_type` parameter through its default, its in-body assignment and
every call site), and fails when the two sets differ **in either direction**:

* a `workspace.*` type in the catalog nothing here requests is a letter nobody
  can receive — run against the pre-fix source, the gate names
  `workspace.invitation.new_user`, i.e. it would have failed the day the type
  landed;
* a type this module requests that the catalog does not carry is the same bug
  mirrored — `request_notification` logs the unknown type and drops it, so the
  caller sees success and the recipient sees nothing.

The catalog is read from the installed sibling, never copied here; CI installs
stapel-notifications `--no-deps` as a test-only sibling, the shape already used
for stapel-profiles.

### Added — a resend cooldown, because there was no limit of any kind

`POST <ws>/invitations/<id>/resend` had no cooldown and no record of the
previous send to build one from. One admin holding `members.invite` could sit
on the endpoint and drive an unbounded number of letters at one address through
this fleet's mail infrastructure — and, with the old unconditional rotation,
churn the invitation credential on every pass.

`WorkspaceInvitation.last_sent_at` (new nullable column, migration 0007) is
written whenever a letter is handed to the mailer.
`STAPEL_WORKSPACES["INVITATION_RESEND_COOLDOWN_SECONDS"]` (default `600`, the
same ten minutes the product's room-invite path already carries) refuses a
resend inside the window with `429 error.429.invitation_resend_cooldown`,
carrying `retry_after` as an error param and as a `Retry-After` header so a
screen can disable its own button and count down instead of teaching the admin
about the limit by failing.

It is a **duration, not a DRF rate** like the neighbouring
`INVITATION_THROTTLE`, and the window belongs to the invited **address**, not
to the calling admin: ten admins are ten scoped-throttle buckets and one
mailbox, so the clock is read across every invitation for that address in the
workspace. It is claimed **inside the row lock**, before the send — two
simultaneous presses would otherwise both pass a check neither had written to
yet. A malformed value is `stapel_workspaces.E009` at `manage.py check` rather
than a limiter that silently turns itself off.

### Changed — a resend no longer kills the link already in the invitee's mailbox

Token rotation on resend is now off by default, behind
`STAPEL_WORKSPACES["INVITATION_ROTATE_TOKEN_ON_RESEND"]`. The reasoning is
written out at that key in `conf.py`; the short form is that a resend goes to
the **same address** as the original letter, so rotation moves the credential
from one letter in that mailbox to another letter in the same mailbox and
narrows nothing — while costing the invitee a dead link in exactly the letter
they are most likely to click ("they say it never arrived" → they then find the
first one). When a token is believed leaked the answer is revoke + re-invite,
which already exists and now leaves a `revoked_by` trail.

No response body changes: the invite token has never appeared in any API
response. A deployment that wants the old behaviour sets the key to `True`.

### Fixed — an invitation can now say who revoked it

`revoked_by` existed only in the emitted `workspace.invitation_revoked` event,
so any UI could show *when* a permissioned action happened and never *by whom*
— a bus message is not a record this service can be asked a question about
afterwards. `WorkspaceInvitation.revoked_by` (new nullable FK, `SET_NULL`,
migration 0007) follows `invited_by`'s existing provenance shape on the
opposite transition, and `InvitationResponse` carries `revoked_by_id` alongside
`revoked_at`. The event still carries the actor too; the two are meant to agree.

### Notes for upgraders

Migration `0007_invitation_revoked_by_and_last_sent_at` is expand-only: two
nullable columns, no backfill. `last_sent_at` is NULL on every pre-existing
row, which reads as "no letter recorded" and therefore "no cooldown owed" — an
invitation created before the upgrade is resendable immediately, exactly as it
was.

`InvitationResponse` gains `revoked_by_id` and `last_sent_at`; both are
additive.

## [0.22.1] — 2026-08-09

### Added — Spanish ships as a language of the library, not as a host override

`translations/errors.es.json` (67 keys) + `docs/errors.es.md`, generated by the
same contour that produces Russian — no hand-written JSON, no product-side
override file. 54 values are lifted verbatim from the curated
`stapel-translate` builtin corpus (`origin: seed:stapel-builtin`), which is what
"clients don't spend tokens" means in practice: the corpus was paid for once.
The remaining 13 are machine translations of this module's own keys, recorded
`origin: llm` — **unreviewed**, and counted as such by the gate now that
stapel-core 0.20.1 stopped treating a curated corpus as human sign-off. Nobody
has read these; `translate_catalogs --approve` is the state transition that
changes that, and it has not been run.

Register and terminology follow the corpus rather than being invented per
module: informal *tú* address, *espacio de trabajo*, *llave de acceso*, *nombre
para mostrar*.

The harness in `tests/test_error_i18n.py` is now language-generic — a language
is a tag in `LANGUAGES` plus whatever the corpus does not carry, and the
catalog, the provenance sidecar, the reference page and the gate all follow.
Adding the next language is not a second copy of this work.

### Fixed — the translation catalogs were built into the wheel and then left out of it

`translations/errors.ru.json` has been in this repository since the i18n wave,
and it has never reached anyone who installed the package. `[tool.setuptools.package-data]`
listed `schemas/`, `migrations/`, `docs/` — and not `translations/`, so setuptools
dropped the directory on the way into the wheel. Verified by installing the built
wheel into an empty virtualenv: no `translations/` under the installed package,
and `load_app_catalogs()` therefore found nothing to merge. Every host running
this module in Russian was silently reading the English canon.

`translations/*.json` is now declared, and the check is the install, not the
manifest: the wheel is built, installed into a clean virtualenv, and the
catalogs are listed on disk.

## [0.21.0] — 2026-08-09

### Changed — the roster's name write goes over comm, and the dotted-path seam is gone

Same URLs, same request and response bodies, same error keys. Two things are
different: the write now works where stapel-profiles is its own container, and
a deployment that has not wired it up is told so in those words instead of
being advised to wait.

0.19.0 shipped `PATCH <ws>/members/<user_id>/name` writing stapel-profiles'
`Profile.display_name` through an **in-process seam**: ask Django's app
registry whether `stapel_profiles` runs here, then resolve
`validate_display_name`, `get_profile_model` and `publish_profile_changed` by
dotted path. That works in a monolith and nowhere else. In a split deployment —
ironmemo's actual topology, where `iron-profiles` is its own container — the
endpoint answered `error.503.profiles_unavailable` **permanently**, with a
`wait_and_retry` hint for a module that was never coming. It was also the
fleet's only cross-module symbol resolution; `stapel-tools` 0.32 minted a lint
rule (SWAP003) for exactly this shape, and after this release this package has
zero runtime hits.

The verdict (`tasks/who-owns-the-name-write.md`) keeps the endpoint here —
authority is a workspaces question, and "only an owner renames an owner" is
rank semantics no other module can evaluate — and changes the transport:

- **stapel-profiles >= 0.10 publishes `profiles.set_display_name`** and
  workspaces calls it. Precedent, not invention: `billing.debit` is the same
  shape (another module initiates a write the data owner performs, on the
  caller's authority) and was itself promoted from an internal HTTP view to a
  comm Function. Everything that belongs to profiles now runs *in* profiles —
  the name canon, the swappable-model discipline, get-or-create, and the
  `profile.changed` emission. This module reimplements none of it.
- **Deleted, not deprecated:** `profiles_in_process`, `_profile_model` and
  `display_name_canon` are gone. Keeping a dotted-path fallback beside the comm
  call would have preserved exactly the topology-dependence being removed.
  `services.py` loses more than it gains.
- **The name canon is asked, not copied.** The serializer calls
  `profiles.validate_display_name` instead of resolving that validator; both
  name-edit endpoints keep the same `error.400.display_name_*` refusals they
  had. Where no provider answers, no canon is applied here and nothing is
  substituted for it — a locally invented rule is the drift this surface exists
  to prevent, and a member rename cannot escape the canon anyway because the
  write re-runs it inside profiles.
- **The roster read moved too**, to `profiles.display_names`. One mechanism now
  covers both topologies (in-process in a monolith, the configured route in a
  split deployment); the `PROFILES_SERVICE_URL` HTTP batch stays as a fallback
  for deployments wired that way before profiles published a read function.

### Added — an unconfigured route fails loudly, as a configuration fact

`error.503.profiles_not_configured`, remediation **`contact_support`**, raised
when there is neither a provider nor a comm route for `profiles.set_display_name`.
The pre-existing `error.503.profiles_unavailable` keeps `wait_and_retry` and now
means what it says: the call was made and it failed.

The split follows the env-address-class v2 canon — an environment error may
self-heal and is worth retrying; a *configuration* error is deterministic, is
fixed only by editing this deployment, and must degrade loudly rather than pose
as a transient outage. The status stays 503 so a live consumer's status
handling does not shift under it; what changes is the key, the remediation and
an ERROR-level log line naming the fix.

Paired with a startup check, `stapel_workspaces.W001`, modelled on
stapel-core's CDN route check (E002) — the fleet's existing way to report a
comm route that is needed and not configured. W and not E deliberately: this is
one endpoint of many, and a dependency serving part of a process's surface
degrades loudly rather than blocking the start of everything else.

**Deployment floor: stapel-profiles >= 0.10** wherever the roster's member
name-edit endpoint is served.

## [0.20.0] — 2026-08-09

### Added — the person says where home is, and the server remembers it

`STAPEL_WORKSPACES["DEFAULT_WORKSPACE_ID"]` shipped in 0.18 describing itself
as "a DEFAULT, not a cage: a person still switches spaces, and their explicit
choice wins over it" — and there was nowhere for that choice to be written
down. So every client invented the rule instead, and the invention that
shipped was `workspaces[0]` off a list ordered by `-last_accessed_at`: the
owner's four pending invitations sat in the org workspace while his screen
showed his personal one (#239).

This is the missing half.

- `PUT /workspaces/api/v1/me/preferred-workspace` records the choice;
  `DELETE` clears it. Both answer with the resulting
  `preferred_workspace_id`, because the client's whole job afterwards is to
  re-resolve.
- `WorkspaceListResponse.preferred_workspace_id` echoes it back on the
  response the client already fetches — no second round trip to learn where
  home is, and no window in which the list has arrived but the answer has
  not. Echoed under exactly the rule `default_workspace_id` already follows:
  only while the caller holds an ACTIVE membership in it, else `""`.
- Stored as `WorkspaceMember.is_preferred` (migration 0006), with a partial
  unique constraint "at most one preferred membership per user" — a database
  invariant, not a convention, so two devices switching at the same moment
  cannot both leave a flag set.

A flag on the membership rather than a user-level column, and that choice is
the feature: the preference dies with the membership row. Remove a member and
the pointer leaves with them — no cleanup job, and no way to be sent on login
to a workspace you can no longer open. Suspension is reversible and leaves the
row, so the flag survives it while the echo goes quiet, and comes back when
the suspension lifts.

Deliberately NOT `last_accessed_at`. That column is telemetry written as a
side effect of a GET; reading it as "the active workspace" is what produced
#239 in the first place. A choice is stated, never inferred from where
somebody last clicked.

Deliberately NOT a field on stapel-profiles either, though that module is the
fleet's user-scoped preference surface. Only this module can validate the
preference against real membership (profiles may not import it), an
unvalidated preference is exactly the stale-pointer defect being closed here,
profiles' `field_defs` entries are opt-in per product so a framework-level fix
riding one would not be present everywhere, and in a split topology it would
make the workspace picker call a second service to persist a workspace choice.

Refused: `error.404.workspace_not_found` for a workspace that does not exist,
one the caller is not in, one whose invitation is still pending, and one where
the membership is suspended — one identical answer, so the endpoint cannot be
used to probe which workspace ids are real.

## [0.19.0] — 2026-08-09

### Added — the roster can fix a name, and there is still only one name canon

Absorbed from meettoday, which had built it as a project-layer overlay mounted
over this module's own URL prefix. Two PATCHes an owner/admin uses to correct
how a person is shown, without waiting for that person to do it themselves —
a typo in the name an admin typed at invite time, a legal-name change, a
provisioned account created as `user-4831`:

- `PATCH <ws>/members/<user_id>/name` writes the **canonical** name,
  stapel-profiles' `Profile.display_name`. Deliberately not
  `WorkspaceMember.display_name_hint`: that column is a pre-profile
  placeholder which goes dark the moment a real profile exists, so a
  "correction" written there is one the renamed person never sees.
- `PATCH <ws>/invitations/<invitation_id>/name` writes the pending
  invitation's `display_name_hint` — the same correction one step earlier,
  for somebody who has not accepted and therefore has no profile row at all.
  Before this, the only fix for a typo in an invitee's name was to revoke and
  re-invite, which re-mails the person.

Both are gated on `members.role.change`, and share it on purpose rather than
taking the invitation surface's `members.invite`: the member's name and the
pending invitation's hint are the same name on either side of acceptance (the
hint is copied onto the membership at accept). A registry that split the two
would let a custom role fix a name that silently reverts the moment the person
accepts. Only an owner may rename an owner — the same hardcoded owner
protection role changes, removals and password resets carry. Both views
declare `ANONYMOUS_DENIED`; a guest holds no membership anywhere, so the
capability check refuses before any row of anybody's is read.

**The name canon stayed where it belongs.** The imported implementation
hand-rolled its own validation — `strip()`, a 35-character ceiling and a
freshly minted `error.400.display_name_too_long`. stapel-profiles already
declares `validate_display_name` as the display-name canon and says, in its
own `llms.txt`, that any host onboarding form, admin action or importer that
writes a name must run it through there instead of inventing a second,
differently-strict regex. So these endpoints call that validator and let its
refusals out verbatim: `error.400.display_name_{too_short,forbidden_chars,`
`invisible_chars,emoji}`, re-declared in this module's registry with the same
English and the same remediation so the contract is honest about what the
endpoints answer with — and with no fifth rule of our own behind them. The
35-character ceiling is a storage fact both columns declare, enforced as the
serializer field's `max_length` and reported as the fleet-standard
`error.400.field.max_length`. Shipping a second, weaker name validation inside
the framework that canonizes the first is the exact defect class this fleet
keeps paying for.

**The seam is the existing one, extended.** Stapel modules never import each
other, and stapel-profiles registers no comm Function and publishes no
write-somebody-else's-name operation — so the mechanism is the same in-process
resolution `_fetch_profile_display_names` already used: `profiles_in_process()`
asks Django's app registry whether stapel-profiles runs in this process and
only then resolves the symbol by dotted path. Three new service helpers on
that seam: `profiles_in_process()`, `display_name_canon()` and
`set_profile_display_name()`. The write also publishes `profile.changed`,
which profiles' own docs demand of any write that does not go through its
serializers — the imported implementation did not, and every downstream
consumer of the name would have desynced silently.

Where stapel-profiles does not run in the process, the member endpoint answers
`error.503.profiles_unavailable` (new key, `wait_and_retry`) instead of a 200
over a write that did not happen. The invitation endpoint keeps working there:
its column is local, and what it loses is the canon — leaving exactly the rule
this module already applied to the same field at invite time, the column
ceiling.

### Changed

- The profiles seam's in-process read now resolves the profile model through
  stapel-profiles' own `get_profile_model` instead of
  `apps.get_model("stapel_profiles", "Profile")`. A host that assembled an
  extended Profile (`STAPEL_SWAP["PROFILES_PROFILE_MODEL"]`) keeps its names
  there; the zero-field default would have answered "nobody has a name"
  forever. Same SWAP001 discipline profiles states for itself.
- `WorkspaceInvitationActionView` takes its capability from a class attribute
  (`capability`, still `members.invite` for revoke/resend) so the name-edit
  PATCH can reuse its workspace-scoped resolution — an unknown invitation id
  and one belonging to another workspace stay one identical 404.

### Testing

The suite gained a second, opt-in session:
`STAPEL_WORKSPACES_TEST_PROFILES=1 pytest tests/test_profiles_comounted.py`
runs with stapel-profiles genuinely mounted next to this module, the way a
monolith deploys, and proves the write lands on a real `Profile` row, that
`profile.changed` really fires, and that the read half sees what the write
half wrote. It is a separate session because co-mounting the sibling registers
ITS error keys into the process-global registry this module's i18n catalog and
error-key gates read — which would corrupt this module's own contract
artifacts to test one seam. CI runs both sessions.

## [0.17.0] — 2026-08-07

### Added — `DEFAULT_WORKSPACE_ID`: the instance names its default, clients stop guessing

Measured on the meettoday stand (2026-08-06). The frontend took
`workspaces[0]` — literally the first row of a list this API orders by
`-last_accessed_at` — as "the active workspace". A person belonging to two
spaces therefore landed in whichever they had touched last. The owner's four
pending invitations sat in the org space while his screen showed his PERSONAL
one, and it reached us as "the owner cannot see his own invitations".

Nothing was broken in invitations: the rows were there, the mandate was there,
the page was mounted. The client had to answer "which workspace?" and no one
had ever told it.

`STAPEL_WORKSPACES["DEFAULT_WORKSPACE_ID"]` names that answer once, on the
server. The workspace list response now carries `default_workspace_id`, and
carries it **only when the caller actually holds an active membership in it** —
pointing a client at a space it cannot open would trade one wrong screen for
another. Unset (the default) yields `""`: a deployment that declares nothing
gets no guess, not a guess of ours.

It is a default, not a cage — an explicit choice by the person still wins, and
making that choice is the client's half of the job.

Regression pinned in tests: `w.id` is a `UUID` and the setting is a `str`, and
`UUID(...) == "a8bb…"` is `False` in Python. Compared naively the key would
have silently never matched — the same shape of defect it exists to remove.

## [0.16.2] — 2026-08-05

### Fixed

- The stapel-profiles seam now looks in-process before it looks over HTTP.
  `_fetch_profile_display_names` read `PROFILES_SERVICE_URL` and returned
  `{}` on the first line when it was unset — which is always, in a monolith:
  nobody points a service at itself. So a deployment with `stapel_profiles`
  right there in `INSTALLED_APPS` never found a name, and every caller
  degraded to a bare email address.

  Measured live on meettoday (2026-08-05). The product had already grown a
  workaround for it — a `profile.changed` subscriber copying `display_name`
  into `User.first_name` purely so Django's `get_full_name()` would fire —
  written, per its own docstring, because "you cannot patch the library, it
  is installed from a package". The library was the right place; it just
  could not see a module sitting next to it. A cross-service seam that only
  knows how to make an HTTP call is half a seam.

## [0.16.1] — 2026-08-05

Два дефекта со стенда (sandbox.meettoday.app): приглашение существующему
пользователю не доставлялось письмом, и когда доставлялось — имя
приглашающего иногда оказывалось сгенерированным логином.

### Fixed — приглашение уже зарегистрированному пользователю теперь всегда доставляется

`_send_invitation_notification` адресовало найденного приглашённого
ТОЛЬКО через `user_id`. stapel-notifications резолвит адрес из СВОЕЙ
таблицы `UserContact` лишь когда запрос не несёт `email` напрямую — а
явный `email` эту таблицу обходит
(`recipient_email = email or (contact.email if contact else None)`).
Аккаунт без строки в `UserContact` (заведённый до появления таблицы или
любым путём, который её не пишет) был недостижим: `POST .../invite`
отвечал 201, приглашение создавалось, письмо не уходило никогда — и
ничего об этом не сообщало. Теперь `email` уходит ВСЕГДА (это единственное,
что эта функция знает точно), а `user_id` — дополнительно, когда
приглашённый уже существует, чтобы notifications применило его язык/пуш/
предпочтения. Резолвится в одной точке (`_send_invitation_notification`),
общей для первого приглашения и `resend`.

### Fixed — имя приглашающего в письме больше не бывает логином

Цепочка была `get_full_name() → username → email` — `username` в этом
флоте генерируемый (`u-xxxxxxxx`), и адресат мог получить письмо вида
«вас пригласил u-8f2a1c». Канон имени — `display_name` из stapel-profiles
(0.16.0). Письмо теперь берёт имя оттуда через уже существующий
best-effort HTTP-батч (`_fetch_profile_display_names`, тот же, что кормит
`MemberResponse.display_name` — переиспользован, не продублирован), и
только если профиля/имени нет — падает на `get_full_name()`, а затем на
почту. `username` из цепочки убран целиком: генерируемый логин не
показывается человеку никогда.

## [0.16.0] — 2026-08-04

Имя участника (аудит фронта миттудея): приглашение с полем «Имя» никуда не
доезжало, а список участников не мог показать ничего, кроме почты.

### `display_name` в приглашении и в ответе об участнике

- `MemberInviteRequest` принимает необязательный `display_name` («Имя» в
  модалке приглашения) — старый вызов без него работает как раньше.
  `MemberResponse` теперь отдаёт `display_name`.
- Имя пользователя НЕ дублируется в этом модуле — оно живёт в
  stapel-profiles (docs/llms.txt). Здесь хранится только ПОДСКАЗКА,
  типизированная при приглашении/провижининге — новое поле
  `display_name_hint` на `WorkspaceInvitation` и `WorkspaceMember`,
  скопированное на участника ровно один раз, при создании (повторное
  принятие приглашения его не перезаписывает).
- `MemberResponse.display_name` предпочитает живой ответ stapel-profiles
  (`POST /profiles/api/v1/batch`, best-effort HTTP через флаг-настройку
  `PROFILES_SERVICE_URL` — та же плоская конвенция, что и `FRONTEND_URL`)
  и падает на `display_name_hint`, когда profiles не установлен,
  недоступен или ещё не знает имени. Ни импорта `stapel_profiles`, ни
  comm Function — модуль профилей их не регистрирует; используется
  `stapel_core.django.peers.service_answered`, чтобы роутинговый 404
  не читался как «имени нет».
- `ProvisionMemberRequest.display_name` (уже существовавшее поле)
  теперь тоже оседает в `display_name_hint` — та же подсказка, тот же
  повод её показать.
- `GET .../invitations` — админский список ожидающих — тоже отдаёт
  `display_name` на каждой строке.

### Известный смежный разрыв — не в этом модуле

Для НЕЗАРЕГИСТРИРОВАННОГО приглашённого (claim-флоу,
`issue_invitation_login_grant` → `auth.issue_login_grant` →
`POST /grant/exchange/`) имя не долетает до профиля даже после этого
релиза: грант в stapel-auth (`LoginGrantService`) не несёт
`display_name` — ни в схеме `auth.issue_login_grant`, ни в кэше гранта,
ни в вызове `_notify_user_registered()` на exchange. Это ровно тот же
`display_name`, который уже проходит по пути `auth.provision_user`
(`_notify_user_registered(display_name=...)` там вызывается). Правка
нужна в stapel-auth (три места, все указаны в services.py рядом с
`issue_invitation_login_grant`), не здесь — вне мандата этого релиза.

## [0.15.0] — 2026-08-03

Мандатная модель миттудея: посадка «с улицы», гость как состояние, rank-гард
инвайтов, уровни капабилити наружу (org-program #85/#87, мандатная-модель
вердикт архитектора 2026-08-03).

### Ось политики посадки — `resolve_landing_workspace`

До этого релиза единственным примитивом был `ensure_personal_workspace` —
безусловный: каждый подписчик на `user.registered`, который его звал,
делал «стать OWNER персонального воркспейса» неизбежной судьбой любой
регистрации. `services.resolve_landing_workspace(user, *, origin)` —
канон, который продукт зовёт ВМЕСТО этого:

- `origin="invited"` — no-op (`None`); членство создаётся отдельным
  механизмом, `accept_invitation`, независимо от значения оси.
- любой другой origin (`"street"`, `"anon"`, ...) — читает новую настройку
  `STAPEL_WORKSPACES["STREET_LANDING_MODE"]`: `"personal"` (дефолт,
  побайтово прежнее поведение) зовёт `ensure_personal_workspace`; `"none"`
  не создаёт ничего — аккаунт садится гостем до инвайта. Нераспознанное
  значение падает в сторону `"none"` (fail-closed).

**Дефолт `"personal"` — обязательное требование, не гипотеза**: на этой же
версии стоит второй продукт (айронмемо), которому мандатная модель НЕ
нужна — «персональные воркспейсы + автосоздание» остаётся его дефолтным
поведением. Бамп до этой версии не требует от него ни новой настройки, ни
миграции, ни правки подписчика: `STAPEL_WORKSPACES` он не трогает вовсе, и
единственная точка входа (`management/commands/consume_auth_events.py`,
которую он же и гоняет отдельным процессом) теперь зовёт канон вместо
`ensure_personal_workspace` напрямую — с дефолтной настройкой результат
побайтово тот же.

Встроенный bus-консьюмер (`consume_auth_events`) — единственная точка
входа оси для микросервисного деплоя без продуктового подписчика — тоже
переведён на канон; раньше он звал `ensure_personal_workspace` напрямую и
полностью игнорировал бы новую ось.

### Гость — состояние, не роль

`permissions.has_active_mandate(user)` / `is_guest(user)` — «аутентифицирован,
но нет активного мандата ни в одном воркспейсе» (accepted, не suspended, в
несилённом воркспейсе). Намеренно не привязан к workspace_id: участник
организации A остаётся гостем организации B. `WorkspaceListCreateView`
(`GET /`) теперь отдаёт `is_guest` рядом со списком воркспейсов — тот же
предикат на проводе, без второго запроса (вычислен из уже пустого/непустого
списка).

### Rank-гард инвайтов, смены роли и провижена

`members.invite` / `members.role.change` / `members.provision` проверяли
только КАПАБИЛИТИ — «может выдавать роли вообще», не «до какого ранга».
Сегодня это безопасно только потому, что капабилити держат admin(300)/owner
— первая же продуктовая роль ниже admin с `members.invite` (владелец
называет её «менеджер») могла бы выдать роль выше своей собственной.
`capabilities.role_exceeds_rank(role, actor_role)` — новая проверка,
`error.403.role_exceeds_inviter_rank`, поверх существующих
owner-инвариантов (не заменяет их). Тесты `tests/test_rank_gate.py`
воспроизводят дыру с продуктовой ролью ниже admin и фиксируют, что она
красная на старом коде.

### `GET /roles` отдаёт уровни капабилити

`RoleListResponse.capability_levels` — эффективная карта
capability → `"standard"|"high"` (builtins + `STAPEL_WORKSPACES["CAPABILITY_LEVELS"]`
оверлей). Фронту больше не нужен собственный порт реестра
(`workspaces-react/src/model/stepUp.ts`), который рисковал разъехаться с
деплойным оверлеем.

### Добавлено

- `STAPEL_WORKSPACES["STREET_LANDING_MODE"]` — новая ось (capability-config.md
  §2), `"personal"` (дефолт) | `"none"`.
- `services.resolve_landing_workspace(user, *, origin)`.
- `permissions.has_active_mandate(user)` / `is_guest(user)`.
- `WorkspaceListResponse.is_guest`.
- `capabilities.role_exceeds_rank(role, actor_role)`.
- `RoleListResponse.capability_levels`.
- `error.403.role_exceeds_inviter_rank`.
- Публичный экспорт: `resolve_landing_workspace`, `is_guest`,
  `has_active_mandate` (`__init__.py`).

### Изменено

- `management/commands/consume_auth_events.py` зовёт
  `resolve_landing_workspace` вместо безусловного `ensure_personal_workspace`
  — с дефолтной настройкой поведение не меняется.

## [0.14.2] — 2026-08-02

Packaging/docs catch-up, no behavior change:

- Badge canon in README + Python 3.14 classifier (#60b0fcd).
- `docs/llms.txt` — the fifth contract artifact (badge-canon §3), emitted
  by `stapel_tools.llms_txt` and checked by the `make contract-check`
  drift gate alongside schema/flows/errors/capabilities.
- `docs/llms.txt` now shipped in the wheel via `package-data`.

## [0.14.1] — 2026-08-01

Переотправка приглашения — своё письмо (#193, notifications >= 0.6.1).

Ресенд слал тот же тип `workspace.invitation`, выдавая напоминание за
первое приглашение — при том что токен на ресенде ротируется и старая
ссылка мертва, а письмо об этом молчало. Теперь путь переотправки шлёт
`workspace.invitation.reminder` (собственный тип в каталоге
stapel-notifications 0.6.1 — по тому же правилу, что `.new_user`: другое
сообщение, которое хост вправе роутить и шаблонить отдельно). Переменные
те же; письмо честно говорит, что прежняя ссылка больше не работает.

Со старым каталогом нотификаций (< 0.6.1) reminder-тип неизвестен и
письмо ресенда молча не уходит (best-effort дроп с logged error на
стороне нотификаций) — обновлять парой. Письмо
`workspace.member_password_reset` (#110), которое этот модуль уже эмитил
в 0.14.0, той же парой начинает реально доставляться: тип появился в
каталоге нотификаций 0.6.1, на этой стороне изменений не потребовалось.

## [0.14.0] — 2026-07-30

Админ организации сбрасывает пароль участнику (#110).

Сброс пароля, выполненный не владельцем аккаунта, — это захват аккаунта,
сделанный намеренно. Ручка тут на три строчки, а вопросов пять, и каждый
из них — отдельный способ сделать её неправильно. Ответы вкомпилированы,
а не задекларированы.

### `POST <ws>/members/<user_id>/password/reset`

**Кто вправе.** Мандат, а не сессия: право `members.password.reset`
(встроенные `admin` и `owner`), объявленное `high`, — сверху
`@requires_verification(scope="sensitive")` требует свежий степ-ап.
Окружающей куки недостаточно, чтобы отдать чужой аккаунт. Пароль
ВЛАДЕЛЬЦА сбрасывает только владелец — та же захардкоженная защита, что
на смене роли и удалении, иначе админ сбрасывает владельца и наследует
организацию. Учётку staff/суперпользователя auth отказывается трогать
вовсе (`error.403.privileged_account`): админ организации — роль внутри
одного воркспейса, staff — роль над всеми, и первая никогда не должна
быть маршрутом во вторую.

**Узнаёт ли пользователь.** Всегда: письмо
`workspace.member_password_reset` называет воркспейс и админа, который
это сделал. Сброс неотличим от захвата, пока владельцу аккаунта не
сказали, что именно произошло. `notified` в ответе честно сообщает, был
ли вообще канал. **Пароль в письмо не кладётся**: в отличие от
провижена (у аккаунта не было другого входа), участник уже существует и
может иметь свой путь восстановления, а сигнал безопасности, содержащий
внутри себя учётные данные, как сигнал не стоит ничего. Пароль отдаёт
админ, вне полосы.

**Попадает ли пользователь в принуждение.** Да: auth поднимает
`provisioned_user_policies` воркспейса (#90), по умолчанию
`password_change`. Пароль, который знает админ, обязан перестать
работать при первом же использовании — и с auth 0.15.0 это требование
держат все 19 путей выдачи сессии. Явный `[]` в запросе подавляет
принуждение и попадает в аудит-строку auth.

**Не оракул ли это.** Нет. Цель, не являющаяся сбрасываемым участником
ЭТОГО воркспейса — неизвестный UUID, реальный аккаунт не из воркспейса,
участник чужого воркспейса и собственный идентификатор вызывающего, —
даёт один **побайтово одинаковый** 404. Проверка права идёт ДО любого
чтения строки цели, поэтому вызывающий без мандата не узнаёт вообще
ничего. `tests/test_api_member_password_reset.py::TestNotAnExistenceOracle`
сравнивает эти ответы побайтово.

**Логируется ли с указанием актора.** Дважды, намеренно: событие
`workspace.member_password_reset` через транзакционный аутбокс (журнал
активности организации) и собственная строка `AuthAuditLog` в auth с
`actor_id` и `via=admin_reset` (журнал безопасности развёртывания). Ни в
одном нет учётного материала.

Свой собственный пароль — `POST /password/change/` в auth. Эта ручка
работает по ДРУГОМУ человеку и о себе отвечает тем же 404, что и о
незнакомце: иначе держатель степ-апа получает способ сменить свой пароль,
не зная старого.

### Добавлено

- Право `members.password.reset` у `admin` (у `owner` через `*`), уровень
  `high` в `BUILTIN_CAPABILITY_LEVELS`.
- Событие `workspace.member_password_reset` (`workspace_id`, `user_id`,
  `role`, `reset_by`, `sessions_revoked`) — без учётного материала.
- `services.reset_member_password()` — орговая половина шва: какие
  политики, какое событие, и сообщить участнику. Требует
  **stapel-auth >= 0.18**; шов не деградирует в «доложенный успех» —
  недоступный auth даёт честный 503, потому что сброс, о котором доложили
  как об успешном, хуже упавшего: админ передаст участнику пароль,
  который не работает.

## [0.13.0] — 2026-07-30

Требования первого входа — независимые галки, и они доезжают до принятия
приглашения (#90).

`settings.security.provisioned_user_policy` был ОДНОЙ строкой, уезжал в
`auth.provision_user` как `first_login_policy`, а тот писал
`password_change_required=(policy == "password_change"),
mfa_enrollment_required=(policy == "mfa_enroll")` — назвать любое из
требований значило снять второе. Обе галки в модалке приглашения были
невыразимы нигде на этом пути. Они висели инертными именно поэтому, а не
потому, что кто-то забыл их подключить.

И это перестало быть декоративным: stapel-auth 0.15.0 перенёс гейт
первого входа внутрь `_issue_session_tokens` — единственного минтера, через
который проходят все 19 путей выдачи сессии, — с охватом «все пути» по
умолчанию. Поднятый флаг закрывает вход везде, а не только на форме пароля.

### Изменено

- **`settings.security.provisioned_user_policies`** — список вместо строки.
  `password_change` и `mfa_enroll` компонуются. Дообработка старой записи
  без миграции данных: `provisioned_user_policy` (строка) читается, когда
  множественного ключа нет; PATCH принимает обе орфографии. Мусорные
  элементы отбрасываются, а не роняют разбор — он выполняется на КАЖДОМ
  провижене и КАЖДОМ приёме приглашения, и опечатка в одном JSON-блобе не
  должна класть приглашения организации.
- `provision_member` шлёт `first_login_policies` (набор) вместо
  `first_login_policy`. **Требует stapel-auth >= 0.17** для набора; более
  старый auth ответит на неизвестный ключ структурной ошибкой, а не
  тихо — провижен упадёт видимо.

### Добавлено — политики доезжают до приёма приглашения

`accept_invitation` применяет требования организации к вступающему
аккаунту через `auth.apply_first_login_policies`. Два свойства этого шва
несущие:

- **шов не дёргается вовсе**, пока организация не настроила политики.
  Дефолт (`password_change`) существует ради аккаунтов, которые
  организация СОЗДАЛА: пароль там выбрал не владелец аккаунта, и он
  обязан перестать работать. Навязывать его человеку, вступившему со
  своей собственной учёткой, значило бы заставить менять пароль каждого
  нового сотрудника каждой организации, которая никогда не открывала
  экран безопасности. Плюс организация, ничего не настроившая, вообще не
  привязана этой строкой к версии auth;
- **когда политики настроены, а auth не может их поднять — приём
  приглашения падает** (503 / ключевая ошибка auth), и вся транзакция
  откатывается: ни членства, ни израсходованного приглашения, повтор
  после починки auth сработает. Организация назвала предусловие допуска;
  шов, который не может его исполнить, обязан отказать, а не впустить
  неукреплённого участника. Best-effort здесь — ровно та форма, которую
  принимает «контроль безопасности, который тихо перестал работать».

## [0.12.0] — 2026-07-30

Приглашение перестало быть письмом в никуда: админ видит, кто не принял,
и может отозвать или переотправить (#109).

Отправка инвайта была операцией «только на запись». Принятие рождало
строку участника — а всё остальное (письмо не дошло, в адресе опечатка,
человек отказался, инвайт молча протух) из продукта не было видно вообще.
Ростер отвечал на вопрос «кто внутри». На вопрос «кто до сих пор снаружи
и с какого числа» не отвечал никто, и подействовать на этот ответ было
нечем.

### Добавлено

- **`GET <ws>/invitations`** — список приглашений воркспейса,
  anchor-пагинация по `created_at`. Фильтр `?status=`:
  - `pending` (по умолчанию) — живые, действующие, занимающие место:
    ровно «кто ещё не принял»;
  - `never_accepted` — они же плюс отклонённые, отозванные и протухшие
    (аудиторский взгляд: «нет приглашения» и «человек отказался» — разные
    факты и разные следующие шаги);
  - `all` — вся история, включая принятые.

  Плюс `?search=` — подстрока по адресу. Токен приглашения в ответе
  отсутствует: это bearer-секрет, и список, который его несёт, раздаёт
  админу рабочую ссылку входа на каждый адрес, который он когда-либо
  приглашал.
- **`POST <ws>/invitations/<id>/revoke`** — отзыв. Терминальное «нет» со
  стороны организации, зеркало отказа приглашённого; в `status` они
  остаются различимыми навсегда. Занятое место возвращается организации в
  момент коммита — `pending()` считает места, и отозванная строка сразу
  перестаёт стоить денег.
- **`POST <ws>/invitations/<id>/resend`** — переотправка. **Токен
  ротируется**, TTL начинается заново, письмо уходит снова. Осознанно
  принимает ПРОТУХШЕЕ приглашение: сдохший TTL — это сбой доставки, а три
  сохранённых терминальных штампа — это решения. Ротация потому, что
  единственная защита токена — то, что он лежал в одном ящике, а
  переотправляют ровно тогда, когда неизвестно, где оказалась первая
  копия.
- Событие `workspace.invitation_revoked` (`workspace_id`, `invitation_id`,
  `role`, `revoked_by`) — единственная запись о том, КТО отозвал: в строке
  есть `revoked_at`, но нет `revoked_by`. Почта приглашённого в payload
  намеренно отсутствует — событие расходится всем подписчикам, а
  приглашение называет человека, который здесь ни на что не соглашался.

### Мандат и гонка

- Все три ручки гейтятся `members.invite`: мандат, который создаёт
  приглашения, он же ими и управляет. Отдельного права не заводилось —
  роль, которая может приглашать, но не может посмотреть собственные
  приглашения, это различие без применения.
- Отзыв и переотправка — **compare-and-set**, а не слепая запись: вью
  читает состояние, сервис перечитывает строку под `select_for_update()
  .unresolved()`. Между этими двумя чтениями и живёт конкурирующий
  accept; в 0.10.0 эту же гонку чинили с другой стороны (отзыв,
  закоммиченный между проверкой accept'а и его захватом строки,
  проигрывал). Теперь оба конца держит один предикат, и
  `tests/test_api_invitation_admin.py::TestRevokeIsCompareAndSet`
  прогоняет accept внутрь окна.
- Фильтры списка — канонические предикаты `InvitationQuerySet`
  (`pending()` / `never_accepted()`), а не новые написания.
  `test_lifecycle_predicates.py` запрещает рукописную копию колонок;
  `TestFiltersAreTheCanonicalPredicates` фиксирует вторую половину — что
  ответ ручки СОВПАДАЕТ с ответом предиката, а не просто избегает
  запрещённого написания.

### Изменено

- `InvitationResponse` дополнен полями `status` (производная метка),
  `declined_at` и `invited_by_id`. Аддитивно — существующий ответ
  `POST members/invite` не сломан.

## [0.11.0] — 2026-07-30

Каждая вью говорит в собственном исходнике, что можно гостю (#168).

`stapel-core` 0.16 превращает включённую ось `AUTH_ANONYMOUS` в вопрос,
на который этот модуль не отвечал. Гостевая сессия — полноценный
`is_authenticated`, поэтому голый `IsAuthenticated` её пропускает, а
исходник молчал о том, хотели этого или нет. Девять вью здесь молчали
(`stapel_core.adoption` W002 на живом деплое). Молчание убрано; сами
гейты в подавляющем большинстве не менялись — они уже стояли, просто
в теле метода, а не в заголовке класса.

### ⚠️ Гость больше не может создать воркспейс

`POST /workspaces/api/v1/` для анонимной сессии теперь **403** вместо 201.
У воркспейса есть владелец, и `create_workspace` делает им вызывающего;
для организации он же — биллинговый якорь. Анонимная учётка одноразовая
по построению: войти в неё повторно нельзя, и организация пережила бы
единственный аккаунт, который может ею управлять и за неё платить.
Существующий энтайтлмент-шов этого не ловил: без установленного биллинга
он деградирует в «разрешить», а личный воркспейс он не гейтит никогда.

Это единственное поведенческое изменение в релизе — отсюда минор, а не
патч.

### Гостевая поверхность, названная явно

- **`WorkspaceListCreateView` (GET) — на живом гостевом пути**, остаётся
  открытой: шапка приложения спрашивает «в каких воркспейсах я состою» для
  ЛЮБОЙ сессии, гостевой в том числе, чтобы решить, что рисовать. Ответ
  гостю — пустой список: это правда, и это дешевле, чем 403, который шапке
  пришлось бы разбирать отдельной веткой. `ANONYMOUS_ALLOWED`.
- **`RoleListView`** — метаданные деплоя (реестр ролей для `RoleSelect`), не
  чьи-либо данные. Закрывать нечего, а сломать фронт можно.
  `ANONYMOUS_ALLOWED`.
- **`WorkspaceDetailView`, `MemberListView`, `MemberInviteView`,
  `MemberProvisionView`, `MemberDetailView`** — уже закрыты `_capability_check`
  в теле: у гостя нет строки `WorkspaceMember`, значит `membership is None`
  и 403 `forbidden_workspace` до любого обращения к данным.
  `ANONYMOUS_DENIED` — декларация не добавляет гейт, она делает
  существующий читаемым из заголовка класса.
- **`InvitationAcceptView`, `InvitationDeclineView`** — закрыты совпадением
  почты: приглашение персональное, а у анонимной учётки почты нет вовсе
  (подтверждение почты — ровно тот акт, который снимает `is_anonymous`).
  `ANONYMOUS_DENIED`.

Новый `tests/test_guest_surface.py` фиксирует обе половины: что гостю
по-прежнему доступно (список воркспейсов, реестр ролей) и что недоступно.

### Изменено

- Минимальный `stapel-core` поднят до `>=0.16` (релиз с
  `ANONYMOUS_ALLOWED`/`ANONYMOUS_DENIED`).

## [0.10.0] — 2026-07-30

Один канонический предикат жизненного цикла вместо девяти рукописных
написаний (#132).

### ⚠️ Меняет суммы счетов: отклонённый инвайт больше не держит место

Расчёт мест (`member_seats_quantity`, энфорсмент `workspaces.members.max`)
считал «живыми» приглашения, у которых не проставлены `accepted_at` и
`revoked_at` и не истёк TTL. Про `declined_at` — колонку, появившуюся
вместе с флоу отказа в 0.7.0, — это написание не знало. То есть
приглашение, от которого приглашённый **явно отказался**, продолжало
резервировать платное место до самого истечения TTL (по умолчанию 7 дней).

Теперь живое приглашение = `InvitationQuerySet.pending()`, ровно
`InvitationStatus.PENDING`: ни одной терминальной отметки и TTL не истёк.

**Что это значит на практике.** У организаций с отказами расчётное число
мест падает; проверка энтайтлмента начнёт пропускать приглашения, которые
раньше упирались в потолок. Это ровно тот же класс расхождения, что
чинили в 0.9.0 для приостановленных участников, только на другой половине
формулы — и ровно поэтому оба написания больше не пишутся руками.

Что при этом **не** изменилось: живое приглашение по-прежнему резервирует
место, хотя приглашённый ещё ни разу не заходил. Это осознанно (всплеск
приглашений не должен перепрыгнуть план между отправкой и принятием) — и
это то место, где формула модуля расходится с формулой владельца
(«зарегистрирован И активирован в этом месяце; ни разу не заходившие не
считаются»). Ни один тест в репозитории эту сверку не делает.

### Изменения поведения

- **`member_count` в ответе воркспейса больше не считает приостановленных.**
  Единственное место, где приостановленное членство ещё считалось хоть
  где-то: доступ, comm-функции, внутренний API и счёт мест исключают его,
  а карточка воркспейса показывала. Организация из 5 «участников» при
  оплате за 4 — ровно то расхождение, ради которого предикат сводится в
  одно место. Потребителям фронта: число может уменьшиться без каких-либо
  действий с членствами.
- **Отозванное приглашение больше нельзя принять в гонке.** Блокировка
  строки в `accept_invitation` (`select_for_update`) сверяла `accepted_at`
  и `declined_at`, но не `revoked_at`, — хотя у `decline_invitation`
  соседняя блокировка сверяла все три. Отзыв, закоммиченный между
  проверкой состояния во вьюхе и захватом строки, проигрывал гонку, и
  приглашение принималось. Теперь у обоих переходов один и тот же
  compare-and-set (`InvitationQuerySet.unresolved()`).
- **GDPR-удаление названо честно.** `WorkspacesGDPRProvider.delete()`
  удаляет отправленные пользователем приглашения, которые никогда не стали
  членством (`never_accepted()`), — отклонённые, отозванные и истёкшие в
  том числе. Поведение то же, что и было; изменилось только имя предиката:
  «никогда не принято» ≠ «живое», и стирание спрашивает именно первое.

### Added

- **`models.MembershipQuerySet`** — единственное место, где вообще
  пишутся колонки жизненного цикла членства:
  - `.active()` — «может действовать» (принят И не приостановлен):
    comm-функции, внутренний эндпоинт, `permissions.get_membership`,
    собственный список воркспейсов, свипы приостановки, `member_count`;
  - `.accepted()` — принят, **приостановленные включены**. Нарочно НЕ
    авторизационный предикат: только для поверхностей, которым надо
    честно показать приостановленную строку (403
    `membership_suspended` вместо голого «не участник»);
  - `.suspended(reason=…)` — каждый консюмер снимает только свою причину
    (MFA не снимает `account_deactivated` и наоборот);
  - `.holds_seat()` — «занимает платное место». Отдельное имя при тех же
    строках, что у `.active()`, — нарочно: «может действовать» и «стоит
    денег» это разные вопросы с сегодня совпадающим ответом, и пока они
    делили рукописное написание, ответы разъехались (#92). Кто их
    разведёт — правит это тело и пишет там почему.
- **`models.InvitationQuerySet`** — то же для приглашений: `.pending()`,
  `.unresolved()` (без часов — цель compare-and-set у accept/decline),
  `.accepted()`, `.never_accepted()`.
- **`tests/test_lifecycle_predicates.py`** — запирающий тест: набор
  краснеет с именем файла и строки, если сырое `accepted_at__isnull` /
  `suspended_at__isnull` / `declined_at__isnull` / `revoked_at__isnull`
  появится в запросе где-либо, кроме `models.py`. Плюс таблица «место →
  предикат» — по тесту на каждое из бывших написаний.

### Чего этот тест НЕ ловит

Соответствует ли `active()` формуле владельца, не знает ни один тест.
Правило предотвращает только **повторный** дрейф — тот, где копии
перестают соглашаться друг с другом, — после того как человек однажды
перевёл спеку в колонки. Первый, смысловой разъезд ловит только
спеко-производный тест или сквозной сценарий, и ни того ни другого здесь
нет. Конкретно: `last_accessed_at` не участвует ни в одном предикате, а
живой инвайт держит место за того, кто ни разу не заходил.

### Что не тронуто (найдено, оставлено)

- **Два из «девяти написаний» предикатом членства не были**: `gdpr.py`
  фильтрует ПРИГЛАШЕНИЯ (та же колонка `accepted_at`, другая модель,
  другой вопрос). Схлопывание их в `active()` было бы тихой сменой
  поведения — они получили предикаты приглашений.
- **Написаний было не девять, а пятнадцать** (29 сырых kwarg'ов):
  0.9.0 добавил десятое (`suspend_memberships_for_deactivated_user`),
  плюс лифты по причине и compare-and-set'ы приглашений.
- **`Workspace.deleted_at`** под правило не заведён: мягкое удаление —
  одна колонка, а разъезжаться нечему, пока формула не составная.
  Фильтров `deleted_at__isnull=True` в модуле хватает; если появится
  вторая колонка жизненного цикла воркспейса, их надо свести так же.

## [0.9.0] — 2026-07-30

Воркспейсы узнают о деактивации аккаунта (#92). Пара к stapel-auth 0.16
(`user.deactivated` / `user.reactivated`).

### ⚠️ Меняет суммы счетов: приостановленный участник больше не занимает место

`member_seats_quantity()` (энфорсмент энтайтлмента `workspaces.members.max`)
считал места как «принятые членства + живые инвайты», включая
**приостановленных**. Приостановка — это ровно «членство перестаёт
считаться везде»: все пути доступа (`permissions.get_membership`,
`has_capability`, comm-функции, внутренний API) уже фильтруют по
`suspended_at IS NULL`. То есть счёт выставлялся за людей, которых продукт
не пускает, — расхождение с собственной формулой модуля.

Теперь места = **активные** (принятые и не приостановленные) участники +
живые инвайты. Инвайты по-прежнему резервируют место, чтобы всплеск
приглашений не перепрыгнул план между отправкой и принятием.

**Что это значит на практике.** Для организаций с приостановленными
участниками расчётное число мест падает, и проверка энтайтлмента начнёт
пропускать приглашения, которые раньше упирались в потолок. Расхождение
было незаметным, пока приостановки были редкостью (только политика
`no_mfa`); деактивация аккаунтов делает их рутиной, а администратор,
деактивирующий уволившегося, обязан освободить место, а не только логин.
Место возвращается на `user.reactivated` — в этом и смысл обратимого
состояния.

### Added

- **Потребитель `user.deactivated`** (`actions.handle_user_deactivated` →
  `services.suspend_memberships_for_deactivated_user`) — приостанавливает
  **все** принятые членства аккаунта с причиной `account_deactivated`
  (`models.SUSPENSION_ACCOUNT_DEACTIVATED`). В отличие от MFA-потребителя
  это не зависит от политики воркспейса: никакая настройка безопасности не
  делает деактивированный аккаунт допустимым. Мягко удалённые воркспейсы
  пропускаются (отзывать там нечего). Механизм — тот же самый
  `suspend_member` (`suspended_at` + причина, эмит
  `workspace.member_suspended`, сброс кэша членства), а не второй способ
  выключить членство; письма подавлены (`notify=False`): аккаунт только что
  потерял все входы, а формулировка письма всё равно MFA-шная.
- **Потребитель `user.reactivated`** (`actions.handle_user_reactivated` →
  `services.lift_deactivation_suspensions_for_user`) — снимает **только**
  приостановки `account_deactivated`. Приостановка `no_mfa` принадлежит
  MFA-потребителю и остаётся: иначе восстановление аккаунта тихо провело бы
  пользователя без второго фактора обратно в require_mfa-воркспейс. Без
  этого обработчика деактивация была бы ловушкой — гвард сессий снова
  пускает, а продукт пустой.
- **`models.MemberState`** — производный (никогда не хранимый) словарь
  жизненного цикла членства: `invited` / `active` / `suspended` /
  `deleted`, плюс свойство `WorkspaceMember.state`. Заведён именно затем,
  чтобы «приостановлен» и «удалён» нельзя было прочитать одинаково с двух
  nullable-колонок на глаз. `deleted` из свойства недостижим — это
  отсутствие строки.
- JSON Schema потребляемых событий: `schemas/consumes/user.deactivated.json`,
  `schemas/consumes/user.reactivated.json`.

### `user.deleted` (GDPR) остаётся отдельным путём

Административная деактивация обратима и обязана оставить приостановленное
членство, в которое можно вернуться; GDPR-стирание (`user.deleted` →
`WorkspacesGDPRProvider.delete`) удаляет строки и мягко удаляет
принадлежащие аккаунту воркспейсы. Ни один из путей не подменяет другой, и
деактивация не трогает воркспейсы, которыми аккаунт владеет.

### Идемпотентность

Доставка событий at-least-once, поэтому повторная доставка
`user.deactivated` не падает, ничего не эмитит второй раз и **не
перезатирает `suspended_at` первой приостановки**. Членство, уже
приостановленное за `no_mfa`, сохраняет свою причину — и остаётся
приостановленным после восстановления аккаунта, что верно: пробел в MFA не
исчез оттого, что аккаунт вернули.

## [0.8.1] — 2026-07-26

### Added — `error-keys/` is finally mounted

`WorkspacesErrorKeysView` has existed since the port but no `urls*.py` ever mounted it — in
*any* stapel library. stapel-translate's `error_collector` polls
`/{prefix}/api/v1/error-keys/` on every service, so the whole endpoint class
answered 404 from Django's URL resolver and the collector harvested nothing
while reporting a plain `HTTP 404`. It is now mounted in `urls_v1.py` at
`error-keys/` (v1 canon), service/staff-gated as the base view declares.

Deliberately **not** in the contract triad: `ErrorKeysView` sets
`schema = None` and `/error-keys` is on the flows allowlist, so `make
contract` is a no-op diff — this is infrastructure, not product surface.

## [0.8.0] — 2026-07-24

Wave 3 of the workspaces org-program (spec §C1-C3): the workspaces side of
the security harden — org-provisioned (synthetic) users and the
`require_mfa` policy implemented as suspension-not-removal. Pairs with
stapel-auth 0.12 (`auth.provision_user` / `auth.mfa_status` /
`user.mfa_enabled|disabled` emits), stapel-billing 0.5 (`billing.debit`),
stapel-core 0.14 (`requires_verification` step-up) and
stapel-notifications 0.4 (`workspace.provisioned_account` /
`workspace.mfa_suspension` / `workspace.mfa_restored` types).

### Added
- **`POST <workspace_id>/members/provision`** — org-created login/password
  accounts (spec §C1). Full username is
  `{workspace_slug}/{username_local}` (slug is globally unique — orgs
  cannot collide); the account is created by `auth.provision_user` with
  the workspace's first-login policy
  (`settings.security.provisioned_user_policy`:
  `password_change` (default) | `mfa_enroll`) and joins immediately
  (`WorkspaceMember(accepted_at=now, provisioned=True)`, migration
  `0004`). Gate stack in order: HIGH step-up
  (`@requires_verification(scope="sensitive")` — same store as admin
  step-up) → capability `members.provision` (403) → entitlement
  `workspaces.provision_user` (402, degrade-allow without billing) →
  optional `billing.debit` when
  `STAPEL_WORKSPACES["PROVISION_USER_CREDITS"]` > 0 (deterministic
  idempotency key `ws-provision:<uuid>` per provision attempt; refused
  debit → 402). Response `{user_id, username, role, generated_password?}`.
- **Credentials delivery (the email nuance)**: a synthetic account
  normally has NO email — with `email` omitted the letter is skipped and
  the server-generated password is returned in the API response to the
  admin exactly once. With the optional `email` passed (stored UNVERIFIED
  by auth), the `workspace.provisioned_account` letter also carries the
  credentials (`username`, `initial_password` only when generated,
  `login_url`). The generated password never rides any event payload and
  is never logged.
- **Provision errors**: auth's structured failures pass through keyed
  with the status taken from the key (`error.409.username_taken`,
  `error.400.username_namespace_invalid`, `error.400.bad_request`); a
  malformed local part fails fast (before any debit/auth roundtrip) with
  the new `error.400.invalid_provision_username`; auth not wired → honest
  503 `error.503.auth_unavailable` (never degrades to allow).
- **Suspension (spec §C3)**: `WorkspaceMember.suspended_at` +
  `suspension_reason` (migration `0004`, expand-only; canonical reason
  `no_mfa`). Suspension is NOT removal — the row and role stay, but every
  access surface stops counting the membership:
  `permissions.get_membership`/`has_capability`/`require_capability`,
  `workspaces.check_membership`/`check_capability` comm Functions, the
  internal HTTP membership endpoint, and the member's own workspace list.
  The view layer answers the honest 403
  `error.403.membership_suspended {reason}` (membership fetched with
  `include_suspended=True`); the cross-service membership cache is
  invalidated on suspend/unsuspend. `services.suspend_member` /
  `unsuspend_member` are idempotent and emit
  `workspace.member_suspended` / `workspace.member_unsuspended`
  (+ schemas) inside the transactional outbox.
- **`WorkspaceSecuritySettings`** — typed dataclass over
  `Workspace.settings["security"]` (`require_mfa`,
  `provisioned_user_policy`; extra keys pass through for client
  extension). A PATCH whose settings payload carries the `security` block
  additionally gates on capability `workspace.security.manage` + HIGH
  step-up (delegate-method pattern — ordinary PATCHes stay step-up-free)
  and validates the two known keys.
- **require_mfa sweep**: flipping `require_mfa` ON runs a synchronous
  `auth.mfa_status` pass over active members; no strong factor → suspend
  (reason `no_mfa`, emit + `workspace.mfa_suspension` letter). Auth
  unavailable during the pass → members are NOT touched (fail-open by
  suspension — fail-closed would lock out the whole org); the policy
  still saves and the event consumer catches up. Flipping it OFF lifts
  the `no_mfa` suspensions it caused (emit, no letter — the mfa_restored
  wording is about the user enabling 2FA, wrong for a policy drop).
- **MFA event consumers** (`actions.py`, idempotent): `user.mfa_disabled`
  → suspend the user's memberships in every `require_mfa` workspace
  (reason `no_mfa`, emit + mfa_suspension letter); `user.mfa_enabled` →
  lift the user's `no_mfa` suspensions ONLY (other reasons are not MFA's
  to lift; emit + mfa_restored letter). Consumed schemas mirrored in
  `schemas/consumes/`.
- **Member surface**: `MemberResponse` gains additive `provisioned`,
  `suspended_at`, `suspension_reason` (members list shows suspension
  state); `workspace.member_provisioned` emit
  `{workspace_id, user_id, role, provisioned_by}` (+ schema); admin list
  shows/filter the new member state; entitlement seam gains
  `ENT_PROVISION_USER` + `debit_provision_credits`.
- Errors + ru: `error.403.membership_suspended` (`{reason}`, remediation
  `fix_input` — the canonical reason is self-serve and restores
  automatically), `error.400.invalid_provision_username` (`fix_input`).
  Contract triad + capabilities.json + error docs regenerated.

## [0.7.0] — 2026-07-24

Wave 2 of the workspaces org-program (spec §B1-B3): the workspaces side of
the invite flow. Pairs with stapel-auth 0.11 (`auth.issue_login_grant` /
`POST /grant/exchange/`); the frontend flow machine lives at the canonical
`/invite/{token}` route.

### Added
- **Invitation state machine**: `WorkspaceInvitation.declined_at`
  (migration `0003`, expand-only) — decline is the invitee's terminal "no",
  distinct from the workspace's `revoked_at`; derived
  `WorkspaceInvitation.status` property (`InvitationStatus`:
  `pending | accepted | declined | revoked | expired`, stored terminal
  timestamps beat the TTL). Accept/decline/claim share one 400 mapping with
  the same precedence.
- **`GET invitations/<token>`** — AllowAny public preview for the
  `/invite/{token}` page: `{workspace_name, role, email_masked
  (m***@d***.com-style), status, email_registered, expires_at}`.
  `email_registered` (case-insensitive account lookup) steers the frontend
  to login vs claim. Throttled (`ScopedRateThrottle`, scope
  `workspace-invitation`, rate from
  `STAPEL_WORKSPACES["INVITATION_THROTTLE"]`, default `30/min`, `None`
  disables) as an enumeration backstop.
- **`POST invitations/<token>/decline`** — authenticated + email-match
  (personal in both directions, like accept); sets `declined_at`
  (row-locked); a later accept answers 400 `error.400.invitation_declined`.
- **`POST invitations/<token>/claim`** — AllowAny, for
  `email_registered == false` only: valid pending invite →
  `auth.issue_login_grant` `{email, verified_email: true, create_if_missing:
  true, language?}` (Accept-Language hint) → `{grant_token}`. Registered
  email → 409 `error.409.email_already_registered`; auth Function not wired
  → honest 503 `error.503.auth_unavailable` (an invite flow without auth is
  meaningless — this seam never degrades to allow, unlike billing's). The
  invitation is NOT consumed — accept stays a separate deliberate step.
- **Token hygiene**: the invite-flow endpoints carry the bearer token in
  the URL path, so `TokenPathNoLogMixin` suppresses Django's 4xx/5xx
  `request.path` log line (documented `_has_been_logged` contract) — the
  token never reaches the logs from module or framework code (gated by
  test).
- **Settings**: `STAPEL_WORKSPACES["INVITATION_THROTTLE"]` (default
  `"30/min"`).
- **Errors** (+ru, i18n gates green): `error.400.invitation_declined`
  (contact_support), `error.409.email_already_registered` (reauthenticate),
  `error.503.auth_unavailable` (wait_and_retry).
- Public API exports: `decline_invitation`, `issue_invitation_login_grant`
  (services), `ISSUE_LOGIN_GRANT` Function name constant on the module.

### Changed
- **Invite email links the canonical frontend route**
  `{FRONTEND_URL}/invite/{token}` (was `/invitations/{token}/accept`) —
  the pair's `InviteAcceptFlow` mount point (spec §B1).
- `accept_invitation` / accept view honour `declined_at` (row-lock filter +
  400 mapping); contract triad regenerated (3 new operations, 2 schemas,
  3 error keys).

## [0.6.0] — 2026-07-24

Wave 1a of the workspaces org-program (spec §A mandate model + §D2
entitlement seam). Pre-1.0 minor = breaking allowed; the backward
compatibility of the builtin four roles (viewer < member < admin < owner,
same capabilities as the old role thresholds) is the release gate and is
covered by tests.

### Added
- **Settings namespace** `STAPEL_WORKSPACES` (`conf.py`): `ROLES` (role
  registry overlay), `CAPABILITY_LEVELS` (step-up level overlay),
  `INVITATION_TTL_DAYS` (default 7 — the previously hard-coded invite
  expiry), `PROVISION_USER_CREDITS` (used from W3).
- **Mandate model** (`capabilities.py`): `BUILTIN_ROLES` (owner `*` rank
  400 / admin 300 / member 200 / viewer 100 per spec §A1),
  `effective_roles()` (last-wins merge of the `ROLES` overlay; `owner`
  system-protected), wildcard capability matcher (`*`, `prefix.*`),
  `capabilities_for()` / `role_has_capability()` / `role_rank()`,
  `BUILTIN_CAPABILITY_LEVELS` + `capability_level()`. System checks
  (`checks.py`, E001-E008) validate the overlays at startup.
- **Permission layer** (`permissions.py`): `has_capability()` /
  `require_capability()` (accepted memberships only; suspension arrives in
  W3). `role_at_least()` moved from list-index to registry ranks —
  identical answers for the builtin four (gate), custom roles participate
  via `rank`. `ROLE_HIERARCHY` kept as export.
- **comm**: new Function `workspaces.check_capability`
  `{workspace_id, user_id, capability}` → `{allowed, role}` (+ schema);
  `workspaces.check_membership` response now carries `capabilities: [str]`
  (additive; raw grant strings, wildcards included) — pairs with the
  stapel-core 0.14 consumer helper `require_capability()`.
- **HTTP API**: `GET /workspaces/api/v1/roles` (authenticated) → the
  effective registry `[{role, rank, capabilities, builtin}]`, rank-desc;
  `WorkspaceResponse.my_capabilities` (additive) on list/detail.
- **Member lifecycle emits** (spec §A4, transactional outbox, schemas in
  `schemas/emits/`): `workspace.member_removed` `{workspace_id, user_id,
  role, removed_by}` and `workspace.member_role_changed` `{workspace_id,
  user_id, old_role, new_role, capabilities}` from member DELETE/PATCH.
- **Entitlement seam** (`entitlements.py`, spec §D2):
  `check_org_entitlement()` / `check_entitlement()` call
  `billing.check_entitlement` (owner-anchored) and degrade to ALLOW on
  `FunctionNotRegistered` / `FunctionRouteNotConfigured` (billing not
  installed — OSS default); a failing installed billing propagates.
  Enforcement: work-workspace creation → `workspaces.org` (402
  `error.402.entitlement_required`); invite + accept →
  `workspaces.members.max` with seats = accepted + live pending invites
  (+ batch size on invite; re-checked on accept) → 402
  `error.402.member_limit_reached {limit}`.
- **Errors** (+ru, i18n gates green): `error.403.missing_capability
  {capability}` (member whose role lacks the capability; not-a-member
  keeps `error.403.forbidden_workspace`), `error.402.entitlement_required`,
  `error.402.member_limit_reached {limit}`.
- Public API exports: capability/entitlement helpers, `CHECK_CAPABILITY`,
  new event names.

### Changed
- **Views enforce capabilities instead of role thresholds** (same outcomes
  for the builtin four): workspace GET → `workspace.view`, PATCH →
  `workspace.update`, member list → `members.view`, invite →
  `members.invite`, role change → `members.role.change`, removal →
  `members.remove`. Owner-only invariants stay hardcoded on the `owner`
  role: workspace delete, granting/changing owner, last-owner protection.
- **Migration 0002** (expand-only): `WorkspaceMember.role` /
  `WorkspaceInvitation.role` `CharField(16)` → `CharField(32)`. Model
  `choices` stay on the builtin four (stapel-recordings `SourceType`
  precedent); serializers validate against `effective_roles()` — custom
  registry roles are invitable/assignable, granting `owner` via invitation
  remains forbidden.
- Invitation expiry reads `STAPEL_WORKSPACES["INVITATION_TTL_DAYS"]`.
- `schemas/emits/workspace.member_joined.json`: `role` enum of the builtin
  four widened to plain string (registry roles join via invitations).
- `stapel-core` floor `>=0.10` → `>=0.14` (comm exceptions contract +
  consumer-side `require_capability` counterpart).

### Notes
- Breaking surface (pre-1.0 minor): member-but-no-capability 403 payloads
  now carry `error.403.missing_capability` (was
  `error.403.forbidden_workspace`); `check_membership` gained a response
  field (additive).
- The monolith aggregate (`stapel-example-monolith`) needs its usual
  post-release `codegen` regen — the byte-identity test compares against
  the released 0.5.4 slice until then.

## [0.5.3] — 2026-07-17

Fix-up #2: 0.5.2's regen still baked the old version into
`docs/capabilities.json` (`make contract` ran before the version bump
landed). Re-ran with 0.5.3 already in `pyproject.toml`; verified match,
suite green.

## [0.5.2] — 2026-07-17

Fix-up: 0.5.1's CI/publish failed on contract drift — `docs/capabilities.json`
embeds the package version and wasn't regenerated for the 0.5.1 bump.
Regenerated via `make contract`; no other diff.

## [0.5.1] — 2026-07-17

Fleet follow-up to stapel-core 0.12.0 (legacy shim sweep). No source
changes needed. Full suite green against core 0.12.0.

### Changed
- `stapel-core` dependency ceiling `<0.12` → `<0.13`.

## [0.5.0] — 2026-07-17

### Removed
- **Legacy Kafka-topic remnants swept** (breaking → minor per house law):
  - `events.TOPIC_WORKSPACE_PERSONAL_CREATED` — alias for the retired Kafka
    topic `stapel.workspaces.personal-created`; no importers anywhere in the
    workspace. Use `EVENT_WORKSPACE_PERSONAL_CREATED`.
  - The duplicate `EVENT_REGISTRY` entry keyed by that alias (registry keeps
    the single canonical `workspace.personal.created` entry).
  - `STAPEL_TOPIC_USER_REGISTERED` env override in `consume_auth_events` —
    existed only for legacy topic layouts; on the bus transport the topic is
    always the action name (`user.registered`), now hard-coded. MODULE.md
    config table and override guidance updated.

## [0.4.4] — 2026-07-17

### Fixed
- `docs/capabilities.json` regenerated again — 0.4.3's release commit ran
  `make contract` before the version bump landed, so the committed file
  still baked in `0.4.2` (`test_capabilities_envelope` caught it in the
  0.4.3 publish retry).

## [0.4.3] — 2026-07-17

### Changed
- `stapel-core` ceiling raised `>=0.10,<0.11` → `>=0.10,<0.12` (core 0.11
  fleet re-pin: default bus, nav, config-checks, error params/language —
  additive for modules).
- `docs/schema.json` regenerated against core 0.11.2 — error object gained
  `error_language` field and a reworded `error` description; no drift
  otherwise.

## [0.4.2] — 2026-07-16

### Changed
- **v1 canon sweep §60** (api-versioning.md §2, §6): `urls.py` renamed to
  `urls_v1.py` (paths inside unchanged); the new root `urls.py` mounts it
  under `v1/` and re-exports `GATE_REGISTRY`. Hosts including
  `stapel_workspaces.urls` under `workspaces/api/` now serve
  `/workspaces/api/v1/...`; bare paths no longer exist (sweep lands before
  the §3 API00x gates are enabled).
- Contract artifacts regenerated (`make contract`): `/v1/` in schema paths.
- `_capabilities.py` canonical_prefix → `/workspaces/api/v1`.
- Lint hygiene to a clean `stapel-verify`: explicit `# noqa` on pre-existing
  findings (R002/R004/R006/R007, CFG001).

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

### Added — member listing: `?search=` + anchor pagination (BACKLOG G12)

`GET /{workspace_id}/members` now supports server-side filtering and cursor
pagination, so every downstream multi-tenant project with a people-picker stops
re-writing its own member listing (the G12 gap surfaced during a client import).

- **`?search=`** — case-insensitive substring match on the member's email **or**
  display name. Display name resolves the way the surface already presents a
  member (it joins `user`): full name → username → email, via a single
  `Coalesce(NullIf(Trim(Concat(first, last))), username, email)` expression.
- **Anchor pagination (stapel-core mandate).** The list is paginated with
  `stapel_core.django.api.pagination.AnchorPagination` — the cursor family that
  is **mandatory everywhere** in Stapel; `limit`/`offset` is banned because its
  windows slip rows (skip/dupe) under concurrent writes. The members endpoint
  now exposes the anchor surface (`anchor` / `limit` / `direction`) and returns
  the anchor envelope (`items`, `next_anchor`, `prev_anchor`, `has_next`,
  `has_prev`, `count`), exactly like the ETALON modules stapel-notifications /
  stapel-tasks (`CreatedAtAnchorPagination`).
- **Sort dropped to the anchor — display-name ordering removed.**
  `AnchorPagination` supports only a **single monotonic** anchor; it has no
  composite (`name,id`) cursor, so a display-name-sorted, insertion-safe window
  is not expressible. Members carry no `created_at`; the analog of the ETALON's
  `-created_at` is **`-invited_at`** (`auto_now_add` — the membership's creation
  timestamp), so the list is now ordered newest-invited-first. Consistency with
  the codebase-wide `limit`/`offset` ban wins over name ordering.
- **Breaking vs the un-tagged, un-published 0.4.0 dev surface only:** the earlier
  in-development shape (`limit`/`offset`, `{"members": [...]}`, stable
  display-name sort) is gone; the now-dead `MemberListResponse` DTO / serializer
  were removed. No released version ever exposed the `limit`/`offset` form.
- **OpenAPI contract:** `search` is declared as an `OpenApiParameter`; the
  `anchor`/`limit`/`direction` params + the `PaginatedMemberResponseList`
  response are emitted by the paginator, so they appear in `docs/schema.json`
  and the frontend codegen sees them — no shadow contract. The monolith
  aggregate's workspaces slice was regenerated in the same change, so the
  byte-identity gate (`test_matches_monolith_workspaces_slice`) stays green.

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
