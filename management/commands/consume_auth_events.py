"""Consume events published by stapel-auth over a broker.

The in-process twin is ``actions.handle_user_registered``; both call
:func:`stapel_workspaces.services.land_registered_account`.
"""
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
        from stapel_workspaces.services import land_registered_account

        user_id = (payload or {}).get("user_id")
        if not user_id:
            self.stderr.write(f"user.registered event missing user_id: {payload}")
            return
        # A broker consumer has no invitation context: every registration is
        # a street one. workspace.personal.created is emitted by the
        # primitive, once, and only when it creates the workspace.
        user, workspace = land_registered_account(payload)
        if user is None:
            self.stderr.write(f"user.registered: user {user_id} not found, skipping")
            return
        if workspace is None:
            self.stdout.write(
                f"user.registered for {user_id}: STREET_LANDING_MODE is not "
                "'personal' — no workspace created, account lands as a guest"
            )
            return
        self.stdout.write(f"Bootstrapped personal workspace {workspace.id} for user {user_id}")
