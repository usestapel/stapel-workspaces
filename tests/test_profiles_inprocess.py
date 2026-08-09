"""The profiles read seam must see a module sitting in the same process.

``_fetch_profile_display_names`` was written as if stapel-profiles were
always a separate service: it read ``PROFILES_SERVICE_URL`` and, finding
nothing, returned ``{}`` on the first line. Nobody points a service at
itself, so in a monolith — profiles right there in ``INSTALLED_APPS`` —
the lookup never found anything and every caller silently degraded to an
email address.

Measured live on meettoday (2026-08-05): ``stapel_profiles`` installed in
the same process, ``PROFILES_SERVICE_URL`` unset everywhere, invitation
emails addressed from a bare email. The product had grown its own
``profile.changed`` subscriber copying ``display_name`` into
``User.first_name`` purely so Django's ``get_full_name()`` would fire —
a workaround for this gap, in the product, where it does not belong.

Since 0.21.0 the branch that closes that gap is a comm call to
``profiles.display_names`` rather than a dotted-path resolution of the
sibling's model factory, so ONE mechanism now covers both topologies: the
transport is in-process in a monolith and the configured route in a split
deployment, and this module never learns which. The HTTP batch stays as a
fallback for deployments wired that way before profiles published a read
function.

These tests pin that branch. They are unit tests on purpose: mounting
stapel-profiles into the DEFAULT suite would register that module's ~50
error keys into the process-global service registry this module's own i18n
catalog and error-key gates read — corrupting this module's contract
artifacts to prove one branch. The genuinely co-mounted run is a second,
opt-in session (``test_profiles_comounted.py``).
"""
import pytest

from stapel_workspaces import services
from stapel_workspaces.services import _fetch_profile_display_names


@pytest.fixture
def _no_service_url(monkeypatch):
    """The whole point: no URL configured, exactly like a monolith."""
    monkeypatch.delenv("PROFILES_SERVICE_URL", raising=False)


@pytest.fixture(autouse=True)
def _clean_function_registry():
    from stapel_core.comm.registry import function_registry

    providers = dict(function_registry._providers)
    schemas = dict(function_registry._schemas)
    yield
    function_registry._providers.clear()
    function_registry._providers.update(providers)
    function_registry._schemas.clear()
    function_registry._schemas.update(schemas)


def _install_profiles_provider(rows):
    """Register ``profiles.display_names`` the way the sibling registers it.

    Faked as the far side of a NAME, not as a symbol of another package:
    that is the seam under test. What the provider does with its own
    swappable model (SWAP001 — a host that assembled an extended Profile
    keeps its names there) is that module's business and is asserted in its
    own suite, not reconstructed here.
    """
    from stapel_core.comm.registry import function_registry

    calls = []

    def _names(payload):
        calls.append(payload)
        return {
            "display_names": {
                uid: rows[uid] for uid in payload["user_ids"] if rows.get(uid)
            }
        }

    function_registry._providers.pop(services.DISPLAY_NAMES, None)
    function_registry.register(services.DISPLAY_NAMES, _names)
    return calls


def test_reads_profiles_in_process_when_no_service_url(_no_service_url):
    """Red before the fix: returned {} because the HTTP branch bailed."""
    calls = _install_profiles_provider(
        {"11111111-1111-1111-1111-111111111111": "Viktor Pasenchuk"}
    )

    got = _fetch_profile_display_names(["11111111-1111-1111-1111-111111111111"])

    assert got == {"11111111-1111-1111-1111-111111111111": "Viktor Pasenchuk"}
    assert calls == [{"user_ids": ["11111111-1111-1111-1111-111111111111"]}]


def test_blank_display_name_is_absent_not_empty_string(_no_service_url):
    """"Missing is not invented" — a blank name must not shadow the hint.

    A caller that got ``{"id": ""}`` would treat it as an answer and stop
    falling back to ``display_name_hint``, so the person would show up as
    nothing at all rather than as the name typed at invite time. Both sides
    hold this: the provider omits blanks, and this side drops them again if
    an older or hand-rolled provider does not.
    """
    _install_profiles_provider(
        {
            "11111111-1111-1111-1111-111111111111": "   ",
            "22222222-2222-2222-2222-222222222222": None,
        }
    )

    got = _fetch_profile_display_names(
        [
            "11111111-1111-1111-1111-111111111111",
            "22222222-2222-2222-2222-222222222222",
        ]
    )

    assert got == {}


def test_no_provider_and_no_url_is_an_empty_answer(_no_service_url):
    """Neither profiles nor a URL: the honest answer is {}, not an error.

    A member's name is cosmetic — never worth failing a roster over. The
    branch has to reach the HTTP fallback's ``{}`` rather than raising
    ``FunctionNotRegistered`` out of a list view.
    """
    from stapel_core.comm.registry import function_registry

    function_registry._providers.pop(services.DISPLAY_NAMES, None)

    assert _fetch_profile_display_names(["11111111-1111-1111-1111-111111111111"]) == {}


def test_a_failing_provider_degrades_instead_of_raising(_no_service_url):
    from stapel_core.comm.registry import function_registry

    def _boom(payload):
        raise RuntimeError("connection reset by peer")

    function_registry._providers.pop(services.DISPLAY_NAMES, None)
    function_registry.register(services.DISPLAY_NAMES, _boom)

    assert _fetch_profile_display_names(["11111111-1111-1111-1111-111111111111"]) == {}


def test_empty_input_is_not_a_call(_no_service_url):
    calls = _install_profiles_provider({})

    assert _fetch_profile_display_names([]) == {}
    assert calls == []
