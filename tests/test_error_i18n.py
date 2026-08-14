"""Localized error catalogs (``translations/errors.<lang>.json``) + provenance gate.

i18n-shipping.md §5 / wave 2. stapel-workspaces applies the reference
``stapel_core.i18n`` catalog contour to the ``errors`` domain (piloted in
stapel-auth): the en canon lives in ``errors.py`` (``register_service_errors``),
each target language ships as a flat
``translations/errors.<lang>.json`` catalog with a shared
``translations/.state.json`` provenance sidecar, and
:func:`check_translation_catalogs` gates coverage, staleness, params and
byte-stability.

Provenance of the localized values (honest, per §5):

* the bulk is **seeded** from the already-curated ``stapel-translate`` builtin
  fixtures (``origin: seed:stapel-builtin``) — requirement 5 ("clients don't
  spend tokens") met by copying the paid-for corpus, not re-running an LLM;
* the handful of keys the fixtures do not cover are **machine translations**
  recorded here per language in :data:`_MACHINE` and written with
  ``origin: llm`` (unreviewed — the gate's W-counter). In a live deployment
  ``translate_catalogs --domain errors --lang <lang> --llm`` produces these
  through the ``STAPEL_I18N["TRANSLATOR"]`` comm seam; offline they come from
  that map so the module regenerates deterministically without a live LLM.

Adding a language is a three-line change here: append the tag to
:data:`LANGUAGES`, add its ``_MACHINE_<TAG>`` table for whatever the corpus
misses, and regenerate. Everything else — the catalog, the provenance sidecar,
the reference doc, the gate — follows.

Regenerate after adding/changing an error key or a translation:

    STAPEL_REGEN_ERROR_I18N=1 python -m pytest tests/test_error_i18n.py::test_regen

then commit ``translations/errors.<lang>.json`` + ``translations/.state.json``
+ ``docs/errors.<lang>.md``. Without the env var the same module is the CI gate.
"""
import io
import os
from pathlib import Path

from django.core.management import call_command

from stapel_core.i18n import (
    check_translation_catalogs,
    source_texts,
    summarize,
    translate_catalog,
)
from stapel_core.i18n.catalogs import load_catalog_file

REPO = Path(__file__).resolve().parent.parent
TRANSLATIONS = REPO / "translations"
DOCS = REPO / "docs"
#: Languages this module ships error catalogs in. en is the canon (the
#: registry literals); every other tag needs a catalog + a docs page.
LANGUAGES = ["en", "ru", "es"]
#: The languages that need a catalog — everything but the source language.
TARGET_LANGUAGES = [lang for lang in LANGUAGES if lang != "en"]

#: stapel-translate builtin fixtures (the curated seed corpus). Overridable for
#: an out-of-tree checkout via STAPEL_TRANSLATE_FIXTURES.
_FIXTURES = Path(
    os.environ.get(
        "STAPEL_TRANSLATE_FIXTURES",
        REPO.parent / "stapel-translate" / "fixtures" / "builtin",
    )
)

#: Machine translations (origin: llm) of the error keys the builtin fixtures do
#: not cover. Both are cross-cutting core keys (network/verification, shared
#: with stapel-auth) — wording matched verbatim to the stapel-auth catalog.
#: All param-free; edit here + regen when the en changes.
_MACHINE_RU = {
    # 0.6 mandate model + entitlement seam (org-program spec §A/§D2).
    "error.403.missing_capability":
        "Ваша роль не включает право {capability} в этом рабочем пространстве",
    "error.402.entitlement_required":
        "Тариф владельца рабочего пространства не включает эту возможность",
    "error.402.member_limit_reached":
        "Достигнут лимит участников рабочего пространства ({limit})",
    # 0.7 invite flow (org-program spec §B2, Wave 2).
    "error.400.invitation_declined":
        "Приглашение было отклонено",
    "error.409.email_already_registered":
        "Аккаунт с этим email уже существует — войдите в него",
    "error.503.auth_unavailable":
        "Сервис аутентификации недоступен; повторите попытку позже",
    "error.503.billing_unavailable":
        "Сервис биллинга недоступен; повторите попытку позже",
    # 0.8 security harden (org-program spec §C1/§C3, Wave 3).
    "error.403.membership_suspended":
        "Ваше членство в этом рабочем пространстве приостановлено ({reason})",
    "error.400.invalid_provision_username":
        "Недопустимое имя пользователя для создаваемого аккаунта",
    # Rank-gard (mandate-model vardict 2026-08-03, org-program #85).
    "error.403.role_exceeds_inviter_rank":
        "Вы не можете выдать роль, которая превышает вашу собственную ({role})",
    # 0.19 roster name edit — the display_name_* keys are borrowed from
    # stapel-profiles and the builtin fixtures already carry their ru
    # wording; only this module's own wiring-gap 503 needs one here.
    "error.503.profiles_unavailable":
        "Сервис профилей недоступен; повторите попытку позже",
    # 0.23 invitation resend cooldown.
    "error.429.invitation_resend_cooldown":
        "Это приглашение недавно отправлялось; отправить снова можно через "
        "{retry_after} с",
    # Single-use login grant (WORK-03).
    "error.429.invitation_grant_pending":
        "Ссылка для входа по этому приглашению ещё действует; запросить "
        "новую можно через {retry_after} с",
}

_MACHINE_ES = {
    # 0.6 mandate model + entitlement seam (org-program spec A/D2).
    "error.403.missing_capability":
        "Tu rol no incluye la capacidad {capability} en este espacio de trabajo",
    "error.402.entitlement_required":
        "El plan del propietario del espacio de trabajo no incluye esta función",
    "error.402.member_limit_reached":
        "Se ha alcanzado el límite de miembros del espacio de trabajo ({limit})",
    # 0.7 invite flow (org-program spec B2, Wave 2).
    "error.400.invitation_declined":
        "La invitación ha sido rechazada",
    "error.409.email_already_registered":
        "Ya existe una cuenta con este correo electrónico: inicia sesión en "
        "su lugar",
    "error.503.auth_unavailable":
        "El servicio de autenticación no está disponible; inténtalo de nuevo "
        "más tarde",
    "error.503.billing_unavailable":
        "El servicio de facturación no está disponible; inténtalo de nuevo "
        "más tarde",
    # 0.8 security harden (org-program spec C1/C3, Wave 3).
    "error.403.membership_suspended":
        "Tu pertenencia a este espacio de trabajo está suspendida ({reason})",
    "error.400.invalid_provision_username":
        "Nombre de usuario no válido para una cuenta aprovisionada",
    # Rank guard (mandate-model verdict 2026-08-03, org-program #85).
    "error.403.role_exceeds_inviter_rank":
        "No puedes conceder un rol superior al tuyo ({role})",
    # 0.19 roster name edit — the display_name_* keys come from the builtin
    # corpus; only this module's own wiring-gap 503s need entries here.
    "error.503.profiles_unavailable":
        "El servicio de perfiles no está disponible; inténtalo de nuevo más tarde",
    "error.503.profiles_not_configured":
        "Este despliegue no tiene configurado un servicio de perfiles, por lo "
        "que aquí no se puede escribir un nombre para mostrar",
    # 0.23 invitation resend cooldown.
    "error.429.invitation_resend_cooldown":
        "Esta invitación se envió por correo hace poco; puedes volver a "
        "enviarla en {retry_after} segundos",
    # Single-use login grant (WORK-03).
    "error.429.invitation_grant_pending":
        "El enlace de acceso de esta invitación sigue siendo válido; puedes "
        "solicitar otro en {retry_after} segundos",
}

#: language -> machine-translation table, consulted for the keys the
#: curated corpus does not carry. Values land as ``origin: llm``.
_MACHINE = {"ru": _MACHINE_RU, "es": _MACHINE_ES}


class _DictTranslator:
    """Offline translator seam — returns fixed machine translations by key."""

    def __init__(self, table):
        self._table = table

    def translate(self, entries, source_language, target_language):
        return {k: self._table[k] for k in entries if k in self._table}


def _seed_from_fixtures(lang: str) -> dict[str, str]:
    """Flat ``{error.*: text}`` seed from the builtin fixtures for *lang*."""
    import json

    path = _FIXTURES / f"{lang}.json"
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {
        k: v for k, v in data.items()
        if isinstance(k, str) and k.startswith("error.")
        and isinstance(v, str) and v
    }


def _regen(lang: str):
    """Materialize one target-language catalog from corpus + machine map."""
    return translate_catalog(
        "errors", lang, TRANSLATIONS,
        source_texts=source_texts("errors"),
        seed=_seed_from_fixtures(lang),
        seed_label="stapel-builtin",
        llm=True,
        translator=_DictTranslator(_MACHINE.get(lang, {})),
    )


def test_regen():
    """Regenerate (env-gated) or assert every catalog is a no-op regen (drift)."""
    if os.environ.get("STAPEL_REGEN_ERROR_I18N"):
        for lang in TARGET_LANGUAGES:
            result = _regen(lang)
            assert not result.missing, f"{lang}: still missing: {result.missing}"
        for lang in LANGUAGES:
            call_command("generate_error_docs", "--lang", lang,
                         "--out", str(DOCS), "--translations", str(TRANSLATIONS),
                         stdout=io.StringIO())
        return

    # Drift gate: regenerating in place (kept, since committed hashes match) must
    # not change any committed catalog.
    for lang in TARGET_LANGUAGES:
        path = TRANSLATIONS / f"errors.{lang}.json"
        before = path.read_bytes()
        _regen(lang)
        assert path.read_bytes() == before, (
            f"errors.{lang}.json drifted — run "
            f"STAPEL_REGEN_ERROR_I18N=1 pytest tests/test_error_i18n.py::test_regen"
        )


def test_catalog_gate_green():
    """E: missing / stale / params-mismatch / not-byte-stable — all zero."""
    issues = check_translation_catalogs(
        "errors", TRANSLATIONS,
        source_texts=source_texts("errors"),
        languages=LANGUAGES,
    )
    errors, _warnings = summarize(issues)
    blocking = [i for i in issues if i.level == "error"]
    assert not blocking, "\n".join(f"[{i.code}] {i.message}" for i in blocking)
    assert errors == 0


def test_every_language_covers_every_key_this_module_owns():
    """Coverage is scoped to OWNERSHIP (stapel-core 0.22.0).

    Core ships its own catalogs now and the loader merges the owner's, so a
    module that also translated core's keys was maintaining a second, drifting
    copy of them — the gate calls that ``foreign`` and fails on it. What this
    module still answers for is every key it owns, in every target language.
    """
    from stapel_core.i18n import owned_keys, owner_of_dir, source_owners

    source = owned_keys(
        source_texts("errors"),
        source_owners("errors"),
        owner_of_dir(TRANSLATIONS),
    )
    for lang in TARGET_LANGUAGES:
        catalog = load_catalog_file(TRANSLATIONS / f"errors.{lang}.json")
        missing = [k for k in source if k not in catalog]
        assert not missing, (
            f"{lang} catalog missing {len(missing)} key(s): {missing[:8]}"
        )


def test_translations_preserve_placeholders():
    """Every localized text keeps exactly the canon's ``{param}`` slots (§3)."""
    from stapel_core.i18n.domains import params_of

    source = source_texts("errors")
    for lang in TARGET_LANGUAGES:
        catalog = load_catalog_file(TRANSLATIONS / f"errors.{lang}.json")
        for key, text in catalog.items():
            if key in source:
                assert set(params_of(text)) == set(params_of(source[key])), \
                    f"{lang}: {key}"


def test_error_reference_matches_a_fresh_regeneration(tmp_path):
    """The committed reference is what the generator produces TODAY.

    ``test_error_docs_exist_for_every_language`` reads the committed file, so a
    reference that had stopped being reproducible stayed green: dropping the
    core-owned duplicates blanked those rows to ``_(en)_`` on the next
    regeneration, and nothing said so until somebody regenerated. stapel-core
    0.23.1 taught the reader to resolve a key this module does not own from its
    owner's catalog; this compares the bytes instead of trusting the file.
    """
    for lang in LANGUAGES:
        call_command("generate_error_docs", "--lang", lang, "--out", str(tmp_path),
                     "--translations", str(TRANSLATIONS), stdout=io.StringIO())
        assert (tmp_path / f"errors.{lang}.md").read_bytes() == \
            (DOCS / f"errors.{lang}.md").read_bytes(), (
                f"docs/errors.{lang}.md is stale — run "
                f"STAPEL_REGEN_ERROR_I18N=1 pytest tests/test_error_i18n.py::test_regen"
            )


def test_error_docs_exist_for_every_language():
    for lang in LANGUAGES:
        path = DOCS / f"errors.{lang}.md"
        assert path.is_file(), f"missing {path}"
    for lang in TARGET_LANGUAGES:
        assert "_(en)_" not in (DOCS / f"errors.{lang}.md").read_text(), (
            f"{lang} error reference has en-fallback rows — "
            f"the {lang} catalog is incomplete"
        )
