"""Consume events published by stapel-auth."""
import os

from stapel_core.bus import BaseBusConsumerCommand, Event

# stapel-auth emits the action through stapel_core.comm; on the bus transport
# the topic is the action name. Override via env for legacy topic layouts.
TOPIC_USER_REGISTERED = os.getenv("STAPEL_TOPIC_USER_REGISTERED", "user.registered")


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
        from stapel_workspaces.services import ensure_personal_workspace
        from stapel_workspaces.events import EVENT_WORKSPACE_PERSONAL_CREATED
        from stapel_core.bus import publish, Event as BusEvent
        workspace = ensure_personal_workspace(user)
        publish(EVENT_WORKSPACE_PERSONAL_CREATED, BusEvent(
            event_type="workspace.personal.created",
            service="workspaces",
            payload={
                "user_id": user_id,
                "workspace_id": str(workspace.id),
            },
        ))
        self.stdout.write(f"Bootstrapped personal workspace {workspace.id} for user {user_id}")
