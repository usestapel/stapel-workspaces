"""Celery app for the e2e host (named celery_app.py, not celery.py: running
e2e/manage.py puts e2e/ on sys.path, where a celery.py would shadow the real
package).

`shared_task` binds to the CURRENT app, and a host that never declares one
gets Celery's default — which does not read Django settings and tries to
reach a broker on localhost. Auth queues a notification task on login, so
without this the first login answers 500 with a connection error.

Everything runs inline: this host has no worker, and an e2e that silently
dropped background work would prove less than it appears to.
"""
from celery import Celery

app = Celery("stapel_workspaces_e2e")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.conf.task_always_eager = True
app.conf.task_eager_propagates = True
app.set_default()
