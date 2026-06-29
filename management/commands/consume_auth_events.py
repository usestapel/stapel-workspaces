"""Consume events published by stapel-auth."""
from stapel_core.bus import BaseBusConsumerCommand, Event

TOPIC_USER_REGISTERED = "stapel.auth.user-registered"


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
            from stapel_core.django.users.models import User
            user = User.objects.get(id=user_id)
        except User.DoesNotExist:
            self.stderr.write(f"user.registered: user {user_id} not found, skipping")
            return
        from stapel_workspaces.services import ensure_personal_workspace
        ensure_personal_workspace(user)
        self.stdout.write(f"Bootstrapped personal workspace for user {user_id}")
