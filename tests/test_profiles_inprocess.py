"""The profiles seam must see a module sitting in the same process.

``_fetch_profile_display_names`` was written as if stapel-profiles were
always a separate service: it reads ``PROFILES_SERVICE_URL`` and, finding
nothing, returns ``{}`` on the first line. Nobody points a service at
itself, so in a monolith — profiles right there in ``INSTALLED_APPS`` —
the lookup never found anything and every caller silently degraded to an
email address.

Measured live on meettoday (2026-08-05): ``stapel_profiles`` installed in
the same process, ``PROFILES_SERVICE_URL`` unset everywhere, invitation
emails addressed from a bare email. The product had grown its own
``profile.changed`` subscriber copying ``display_name`` into
``User.first_name`` purely so Django's ``get_full_name()`` would fire —
a workaround for this gap, in the product, where it does not belong.

These tests pin the in-process branch. They are unit tests on purpose:
installing stapel-profiles into this suite would couple two libraries'
test environments to prove one branch.
"""
import pytest

from stapel_workspaces.services import _fetch_profile_display_names


class _FakeQuerySet:
    def __init__(self, rows):
        self._rows = rows

    def values_list(self, *fields):
        return self._rows


class _FakeManager:
    def __init__(self, rows):
        self._rows = rows
        self.filter_calls = []

    def filter(self, **kwargs):
        self.filter_calls.append(kwargs)
        return _FakeQuerySet(self._rows)


class _FakeProfile:
    def __init__(self, rows):
        self.objects = _FakeManager(rows)


@pytest.fixture
def _no_service_url(monkeypatch):
    """The whole point: no URL configured, exactly like a monolith."""
    monkeypatch.delenv("PROFILES_SERVICE_URL", raising=False)


def _install_fake_profiles(monkeypatch, rows):
    from django.apps import apps as django_apps

    fake = _FakeProfile(rows)
    monkeypatch.setattr(
        django_apps, "is_installed", lambda label: label == "stapel_profiles"
    )
    monkeypatch.setattr(django_apps, "get_model", lambda app, model: fake)
    return fake


def test_reads_profiles_in_process_when_no_service_url(monkeypatch, _no_service_url):
    """Red before the fix: returned {} because the HTTP branch bailed."""
    fake = _install_fake_profiles(
        monkeypatch, [("11111111-1111-1111-1111-111111111111", "Виктор Пасенчук")]
    )

    got = _fetch_profile_display_names(["11111111-1111-1111-1111-111111111111"])

    assert got == {"11111111-1111-1111-1111-111111111111": "Виктор Пасенчук"}
    assert fake.objects.filter_calls == [
        {"user_id__in": ["11111111-1111-1111-1111-111111111111"]}
    ]


def test_blank_display_name_is_absent_not_empty_string(monkeypatch, _no_service_url):
    """"Missing is not invented" — a blank name must not shadow the hint.

    A caller that got ``{"id": ""}`` would treat it as an answer and stop
    falling back to ``display_name_hint``, so the person would show up as
    nothing at all rather than as the name typed at invite time.
    """
    _install_fake_profiles(
        monkeypatch,
        [
            ("11111111-1111-1111-1111-111111111111", "   "),
            ("22222222-2222-2222-2222-222222222222", None),
        ],
    )

    got = _fetch_profile_display_names(
        [
            "11111111-1111-1111-1111-111111111111",
            "22222222-2222-2222-2222-222222222222",
        ]
    )

    assert got == {}


def test_profiles_not_installed_still_falls_through_to_http(monkeypatch, _no_service_url):
    """Not a monolith: the in-process branch must not swallow the request.

    With no app and no URL the honest answer is {} — but it has to be the
    HTTP branch's {}, not an exception from asking for a model that is not
    there.
    """
    from django.apps import apps as django_apps

    monkeypatch.setattr(django_apps, "is_installed", lambda label: False)

    assert _fetch_profile_display_names(["11111111-1111-1111-1111-111111111111"]) == {}
