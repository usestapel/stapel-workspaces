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
        try:
            from django.contrib.auth import get_user_model
            User = get_user_model()
            user = User.objects.get(id=user_id)
        except User.DoesNotExist:
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
            payload={
                "user_id": user_id,
                "workspace_id": str(workspace.id),
            },
        ))
        self.stdout.write(f"Bootstrapped personal workspace {workspace.id} for user {user_id}")
