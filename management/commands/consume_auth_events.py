"""Consume events published by stapel-auth."""
from stapel_core.bus import BaseBusConsumerCommand, Event

# stapel-auth emits the action through stapel_core.comm; on the bus transport
# the topic is the action name.
TOPIC_USER_REGISTERED = "user.registered"


class Command(BaseBusConsumerCommand):
    help = "Listen for auth events and react (e.g. bootstrap personal workspaces)"
    topics = [TOPIC_USER_REGISTERED]
    consumer_group = "workspaces-auth-events"

    def handle_event(self, event: Event) -> None:
        if event.event_type == "user.registered":
            self._on_user_registered(event.payload)

    def _on_user_registered(self, payload: dict) -> None:
        user_id = payload.get("user_id")
        if not user_id:
            self.stderr.write(f"user.registered event missing user_id: {payload}")
            return
        from django.contrib.auth import get_user_model
        User = get_user_model()
        user = User.objects.filter(pk=user_id).first()
        if user is None:
            user = self._mirror_user(payload)
        if user is None:
            self.stderr.write(f"user.registered: user {user_id} not found, skipping")
            return
        from stapel_workspaces.services import resolve_landing_workspace
        from stapel_workspaces.events import EVENT_WORKSPACE_PERSONAL_CREATED
        from stapel_core.bus import publish, Event as BusEvent
        # This bus-transport consumer has no invitation context of its own
        # (unlike a product's in-process subscriber, which may know a
        # pending invite exists for this email) — every `user.registered`
        # it sees is treated as an un-invited ("street") registration, the
        # historical assumption this consumer always made. The landing
        # policy itself now goes through the canon (org-program #85,
        # mandate-model vardict 2026-08-03) instead of the unconditional
        # `ensure_personal_workspace`: with the default
        # STREET_LANDING_MODE="personal" this is byte-identical to before;
        # a deployment that opts into "none" gets what the axis promises —
        # no personal workspace, no event — even when this bundled command
        # (not a product's custom subscriber) is the one wiring the bus.
        workspace = resolve_landing_workspace(user, origin="street")
        if workspace is None:
            self.stdout.write(
                f"user.registered for {user_id}: STREET_LANDING_MODE is not "
                "'personal' — no workspace created, account lands as a guest"
            )
            return
        publish(EVENT_WORKSPACE_PERSONAL_CREATED, BusEvent(
            event_type="workspace.personal.created",
            service="workspaces",
            # The identity fields ride along, and they are not decoration.
            # A consumer of this event is in exactly the position this
            # command was in before 0.30.3 — it needs a local `users` row for
            # the foreign key it is about to write, and its own writer of one
            # (core's JWT seam) has not run, because the account has made no
            # authenticated request to THAT service yet. It can materialise
            # the row from this event, but with `user_id` alone it has to
            # take the model's defaults, so a guest lands as
            # `is_anonymous=False, auth_type="email"` and stays wrong until
            # their first request there repairs it. We have the account in
            # hand; saying who it is costs three keys.
            #
            # Privileges are NOT here, on purpose: an event is not a token
            # and may not mint a local staff account. `ensure_shadow_user`
            # strips them even if a payload carries them.
            payload={
                "user_id": user_id,
                "workspace_id": str(workspace.id),
                "is_anonymous": bool(getattr(user, "is_anonymous", False)),
                "auth_type": getattr(user, "auth_type", None),
                "email": getattr(user, "email", None) or None,
            },
        ))
        self.stdout.write(f"Bootstrapped personal workspace {workspace.id} for user {user_id}")

    def _mirror_user(self, payload: dict):
        """Materialise the local shadow row from the event itself.

        In a microservices deployment nothing in THIS service writes that row
        on registration: it appears when the JWT seam
        (``get_or_create_user_from_jwt``) sees the account's first
        authenticated request here. So two writers race for it, and this
        consumer normally arrives first — the event is published inside the
        registration request, the client's first call to this service is not.
        The old "not found, skipping" was therefore not a rare miss but the
        common case, and it was PERMANENT: the offset commits, nothing
        replays it, and the account has no workspace for the rest of its
        life. Measured on a deployed stand 2026-09-12 — an anonymous enroll
        emitted ``user.registered`` at 12:58:52.55, the shadow row appeared
        at 12:58:53.77, and the guest's every workspace-scoped call after it
        was dead.

        Since 0.31.0 this goes through ``stapel_core``'s one event-facing
        seam, :func:`~stapel_core.django.users.ensure_shadow_user`, rather
        than calling the JWT one directly. The behaviour here is the same —
        this method is only reached when there is no row, so the JWT seam's
        claim-sync could never fire on an existing one — and the point is
        that there is now ONE implementation of "materialise a user from an
        event" for the fleet instead of three (this one, ``billing_ext``'s
        and the one iron-recordings was about to write). What it adds on top
        of the old call: privileges stripped from the payload rather than
        merely omitted from it, a guest's email NULL instead of ``""`` (the
        column is unique — two guests collided), and a username derived from
        the id, so a replayed event proposes the same name twice. The
        deletion and deactivation gates were already there and stay.

        Returns ``None`` in authoritative-user-store mode
        (``JWT_CREATE_USERS_FROM_TOKEN=False``, the default, and what the
        auth service itself runs): there the local database decides who
        exists and skipping the event is the correct answer.
        """
        from stapel_core.django.users import ensure_shadow_user

        return ensure_shadow_user(payload.get("user_id"), payload)
