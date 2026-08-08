"""Форма инстанса отдаётся наружу — и именно тому, кто спейсу никто.

ЗАЧЕМ. Ось ``STREET_LANDING_MODE`` завели 03.08.2026, и она с тех пор
решала главное: получает человек «с улицы» своё пространство
(``personal`` — публичное облако) или приземляется гостем без него
(``none`` — закрытый контур). Но жила ось ТОЛЬКО в окружении бэкенда:
наружу не отдавалась ничем, ни в одном ответе, и на клиенте отличить один
мир от другого было нечем.

Цена — экран после кика из Спейса (запрос Олега, 08.08.2026). В закрытом
контуре человеку идти НЕКУДА: своего пространства у него нет и взяться
неоткуда. Нарисовать ему «создайте встречу» значит нарисовать тупик
кнопкой. В публичном облаке всё наоборот — своё пространство есть, и туда
нужно вести.
"""
import pytest
from django.test import override_settings
from django.urls import reverse

pytestmark = pytest.mark.django_db


def _get(client):
    return client.get(reverse("instance-shape"))


class TestОсьДоезжаетДоКлиента:
    @override_settings(STAPEL_WORKSPACES={"STREET_LANDING_MODE": "none"})
    def test_закрытый_контур_виден(self, api_client):
        response = _get(api_client)
        assert response.status_code == 200
        assert response.data["landing"] == "none"

    @override_settings(STAPEL_WORKSPACES={"STREET_LANDING_MODE": "personal"})
    def test_публичное_облако_видно(self, api_client):
        assert _get(api_client).data["landing"] == "personal"

    def test_дефолт_публичное_облако(self, api_client):
        """Ось не выставлена — поведение до её появления, байт в байт."""
        assert _get(api_client).data["landing"] == "personal"


class TestРегистрацияЭтоТаЖеОсь:
    """Две стороны одного решения — клиенту нужны обе."""

    @override_settings(STAPEL_WORKSPACES={"STREET_LANDING_MODE": "none"})
    def test_в_закрытом_контуре_реги_нет(self, api_client):
        assert _get(api_client).data["registration_open"] is False

    @override_settings(STAPEL_WORKSPACES={"STREET_LANDING_MODE": "personal"})
    def test_в_публичном_облаке_рега_открыта(self, api_client):
        assert _get(api_client).data["registration_open"] is True


class TestАнонимуОткрытоНамеренно:
    def test_без_авторизации_отвечает(self, api_client):
        """Единственный адресат этой ручки — человек БЕЗ доступа к Спейсу.

        Закрыть её авторизацией значило бы закрыть от того, ради кого она
        и заводилась: выброшенного из Спейса или вышедшего самого.
        """
        response = _get(api_client)
        assert response.status_code == 200
        assert set(response.data) == {"landing", "registration_open"}

    def test_не_протекает_ничего_сверх_формы(self, api_client):
        """Ответ — свойство РАЗВЁРТЫВАНИЯ, не данные людей.

        Пришпилено, чтобы поле, добавленное сюда «заодно», не уехало
        анониму молча: ручка публичная, и любое новое поле в ней публично
        по определению.
        """
        payload = _get(api_client).data
        assert list(payload) == ["landing", "registration_open"]
