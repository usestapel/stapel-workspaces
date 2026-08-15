"""End-to-end proof of the workspace deletion lifecycle over real HTTP.

Boots a real host (auth + workspaces on SQLite, in-process comm, outbox on)
and drives deletion the way a client does — including every refusal, because
a refusal that never reaches the client as a KEYED code is a refusal no
screen can render, and that is exactly the class of defect a unit test on
the service cannot see.

    login -> the instance's home workspace refuses deletion (409, keyed)
          -> the detail response ADVERTISED that refusal before the click
          -> a personal workspace refuses deletion (409, keyed)
          -> an ordinary workspace deletes (204), leaves the list, 404s after
          -> deleting it twice is a 404, not a second deletion
          -> a non-owner admin is refused (403) and the workspace survives
          -> the deletion is in the event store: the peer-facing event AND
             the workspace's own audit line

Run:  /Users/apple/Projects/stapel/.venv/bin/python e2e/run_e2e.py
Exit code 0 + "E2E PASS" is the gate; any assertion failure is a real defect
somewhere on the path.
"""
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import requests

REPO = Path(__file__).resolve().parent.parent
STATE = Path(os.environ.get("STAPEL_WORKSPACES_E2E_DIR", "/tmp/stapel-workspaces-e2e"))
PY = sys.executable
PORT = int(os.environ.get("STAPEL_WORKSPACES_E2E_PORT", "8771"))
BASE = f"http://127.0.0.1:{PORT}"
API = f"{BASE}/workspaces/api/v1"

PASSWORD = "e2e-pass-Str0ng!"
OWNER = "e2e-owner"
OTHER = "e2e-other"

ERR_INSTANCE_DEFAULT = "error.409.workspace_is_instance_default"
ERR_PERSONAL = "error.409.workspace_is_personal"


def manage(*args, env_extra=None):
    env = {**os.environ, "STAPEL_WORKSPACES_E2E_DIR": str(STATE), **(env_extra or {})}
    proc = subprocess.run(
        [PY, str(REPO / "e2e" / "manage.py"), *args],
        cwd=REPO, env=env, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        # Surface it: a management command that failed silently is how an
        # e2e turns into a mystery instead of a diagnosis.
        print(proc.stdout)
        print(proc.stderr, file=sys.stderr)
        fail(f"manage.py {' '.join(args)} exited {proc.returncode}")
    return proc


def step(name):
    print(f"--- {name}")


def fail(msg):
    print(f"FAIL {msg}")
    raise SystemExit(1)


def expect(resp, status, name):
    if resp.status_code != status:
        fail(f"{name}: expected {status}, got {resp.status_code}: {resp.text[:500]}")
    return resp


def expect_error(resp, status, code, name):
    """A refusal must arrive with the KEYED code, not merely the status.

    The status tells a client that it failed; the code is the only thing a
    screen can turn into a sentence, and it is what the pair's i18n
    dictionary is keyed by.
    """
    expect(resp, status, name)
    got = (resp.json() or {}).get("localizable_error")
    if got != code:
        fail(f"{name}: expected code {code}, got {got}: {resp.text[:300]}")
    return resp


def session_for(username):
    """Log in through the module's own password endpoint, as a client does."""
    resp = expect(
        requests.post(
            f"{BASE}/auth/api/v1/password/login/",
            json={"login": username, "password": PASSWORD},
            timeout=10,
        ),
        200, f"login {username}",
    )
    body = resp.json()
    access = (body.get("tokens") or {}).get("access")
    if not access:
        fail(f"login {username}: no access token in {json.dumps(body)[:300]}")
    s = requests.Session()
    s.headers["Authorization"] = f"Bearer {access}"
    return s


def create_workspace(s, name, slug, kind="work"):
    resp = s.post(f"{API}/", json={"name": name, "slug": slug, "type": kind}, timeout=10)
    if resp.status_code not in (200, 201):
        fail(f"create {name}: {resp.status_code}: {resp.text[:400]}")
    return resp.json()["id"]


def main():
    step("reset state dir")
    shutil.rmtree(STATE, ignore_errors=True)
    STATE.mkdir(parents=True)

    step("migrate")
    manage("migrate", "--noinput")

    step("bootstrap accounts")
    manage(
        "shell", "-c",
        "from django.contrib.auth import get_user_model as g; U=g(); "
        f"U.objects.create_user(username='{OWNER}', password='{PASSWORD}'); "
        f"U.objects.create_user(username='{OTHER}', password='{PASSWORD}')",
    )

    step("bootstrap the instance's home workspace (an operator's act, before boot)")
    out = manage(
        "shell", "-c",
        "from django.contrib.auth import get_user_model as g; "
        "from stapel_workspaces.services import create_workspace; "
        f"u=g().objects.get(username='{OWNER}'); "
        "print(create_workspace(user=u, name='Home', type='work').id)",
    ).stdout.strip().splitlines()[-1]
    home_id = out.strip()
    print(f"    home workspace = {home_id}")

    step("boot server with the home workspace declared as the instance default")
    env = {
        "STAPEL_WORKSPACES_E2E_DIR": str(STATE),
        "STAPEL_WORKSPACES_E2E_DEFAULT_WS": home_id,
    }
    server = subprocess.Popen(
        [PY, str(REPO / "e2e" / "manage.py"), "runserver", f"127.0.0.1:{PORT}", "--noreload"],
        cwd=REPO, env={**os.environ, **env},
        stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
    )
    try:
        for _ in range(60):
            try:
                requests.get(f"{API}/", timeout=1)
                break
            except requests.RequestException:
                time.sleep(0.25)
        else:
            fail("server never came up")
        run_flow(home_id)
    finally:
        server.terminate()
        server.wait(timeout=10)


def run_flow(home_id):
    owner = session_for(OWNER)
    other = session_for(OTHER)

    step("the instance default advertises its refusal BEFORE the click")
    body = expect(owner.get(f"{API}/{home_id}", timeout=10), 200, "home detail").json()
    if body.get("can_delete") is not False:
        fail(f"home should not be deletable: can_delete={body.get('can_delete')}")
    if body.get("delete_blocked_reason") != ERR_INSTANCE_DEFAULT:
        fail(f"home reason: {body.get('delete_blocked_reason')}")

    step("and refuses the deletion itself with the SAME code")
    expect_error(
        owner.delete(f"{API}/{home_id}", timeout=10),
        409, ERR_INSTANCE_DEFAULT, "delete home",
    )
    expect(owner.get(f"{API}/{home_id}", timeout=10), 200, "home survives")

    step("a personal workspace this instance re-mints refuses too")
    personal_id = create_workspace(owner, "Personal", "e2e-personal", kind="personal")
    detail = expect(
        owner.get(f"{API}/{personal_id}", timeout=10), 200, "personal detail"
    ).json()
    if detail.get("delete_blocked_reason") != ERR_PERSONAL:
        fail(f"personal reason: {detail.get('delete_blocked_reason')}")
    expect_error(
        owner.delete(f"{API}/{personal_id}", timeout=10),
        409, ERR_PERSONAL, "delete personal",
    )

    step("an ordinary workspace is deletable, and says so")
    doomed_id = create_workspace(owner, "Doomed", "e2e-doomed")
    detail = expect(
        owner.get(f"{API}/{doomed_id}", timeout=10), 200, "doomed detail"
    ).json()
    if detail.get("can_delete") is not True:
        fail(f"doomed should be deletable: {detail.get('delete_blocked_reason')}")

    step("delete it")
    expect(owner.delete(f"{API}/{doomed_id}", timeout=10), 204, "delete doomed")

    step("it is gone for reads, and gone from the list")
    expect(owner.get(f"{API}/{doomed_id}", timeout=10), 404, "doomed detail after delete")
    listed = expect(owner.get(f"{API}/", timeout=10), 200, "list").json()["workspaces"]
    if any(w["id"] == doomed_id for w in listed):
        fail("deleted workspace still in the caller's list")

    step("deleting it twice is a 404, not a second deletion")
    expect(owner.delete(f"{API}/{doomed_id}", timeout=10), 404, "second delete")

    step("a non-owner admin is refused, and the workspace survives")
    shared_id = create_workspace(owner, "Shared", "e2e-shared")
    manage(
        "shell", "-c",
        "from django.contrib.auth import get_user_model as g; "
        "from django.utils import timezone; "
        "from stapel_workspaces.models import Role, WorkspaceMember; "
        f"u=g().objects.get(username='{OTHER}'); "
        f"WorkspaceMember.objects.create(workspace_id='{shared_id}', user=u, "
        "role=Role.ADMIN, accepted_at=timezone.now())",
    )
    resp = other.delete(f"{API}/{shared_id}", timeout=10)
    if resp.status_code != 403:
        fail(f"admin delete: expected 403, got {resp.status_code}: {resp.text[:300]}")
    detail = expect(
        other.get(f"{API}/{shared_id}", timeout=10), 200, "shared survives"
    ).json()
    if detail.get("can_delete") is not False or not detail.get("delete_blocked_reason"):
        fail("an admin must be told, on the detail, that deletion is not theirs")

    step("the deletion reached the event store — peers AND the workspace's history")
    out = manage(
        "shell", "-c",
        "import json; from stapel_core import eventstore; "
        f"audit=[e.payload for e in eventstore.query('workspace.audit', "
        f"filters={{'workspace_id': '{doomed_id}'}}, limit=200)]; "
        "print('AUDIT=' + json.dumps([a['action'] for a in audit]))",
    ).stdout
    actions = json.loads(
        [ln for ln in out.splitlines() if ln.startswith("AUDIT=")][-1][len("AUDIT="):]
    )
    if "deleted" not in actions:
        fail(f"no 'deleted' line in the workspace's audit history: {actions}")

    out = manage(
        "shell", "-c",
        "import json; from django.apps import apps; "
        "M=apps.get_model('stapel_outbox','OutboxEvent'); "
        "print('EVENTS=' + json.dumps(sorted(set(M.objects.values_list("
        "'topic', flat=True)))))",
    ).stdout
    events = json.loads(
        [ln for ln in out.splitlines() if ln.startswith("EVENTS=")][-1][len("EVENTS="):]
    )
    if "workspace.deleted" not in events:
        fail(f"peers were never told: outbox actions = {events}")

    print("E2E PASS")


if __name__ == "__main__":
    main()
