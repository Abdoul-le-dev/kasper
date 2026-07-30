"""
Tests isolés du module telegram_notifier.py.

Le formatage des messages est testé sans aucun réseau (fonctions pures).
L'envoi est testé via un transport httpx mocké (httpx.MockTransport) —
aucune requête réseau réelle n'est faite vers api.telegram.org.
"""

import sys
import os
import pytest
import httpx

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from trading_engine import telegram_notifier as tg


# --- Formatage (pur, sans réseau) ---

def test_format_enter_message_contains_key_fields():
    decision = {
        "decision": "ENTER", "direction": "BUY", "entry": 2000.0, "sl": 1995.0, "tp": 2010.0,
        "risque_dollars": 5.0, "rr_vise": 2.0, "scd": 2, "irv": 1.05, "fqe_score": 4,
        "raisonnement": "Entrée validée",
    }
    msg = tg.format_enter_message(decision)
    assert "ENTRÉE BUY" in msg
    assert "2000.00" in msg
    assert "1995.00" in msg
    assert "2010.00" in msg
    assert "5.00$" in msg
    assert "Entrée validée" in msg


def test_format_exit_message_contains_reason():
    decision = {"decision": "EXIT", "raisonnement": "Cassure nette de la zone"}
    msg = tg.format_exit_message(decision)
    assert "SORTIE" in msg
    assert "Cassure nette de la zone" in msg


def test_format_reduce_message_contains_breakeven():
    decision = {
        "decision": "REDUCE", "nouveau_sl": 2000.0, "pourcentage_reduction": 50,
        "raisonnement": "1R atteint",
    }
    msg = tg.format_reduce_message(decision)
    assert "RÉDUCTION" in msg
    assert "2000.00" in msg
    assert "50%" in msg


def test_format_hold_message_contains_scores():
    decision = {"decision": "HOLD", "scd": 0, "irv": 0.9, "fqe_score": 2, "raisonnement": "Pas assez de confluence"}
    msg = tg.format_hold_message(decision)
    assert "HOLD" in msg
    assert "0.90" in msg


def test_format_urgent_alert_message():
    msg = tg.format_urgent_alert_message("Spread anormalement élargi")
    assert "INTERVENTION IMMÉDIATE" in msg
    assert "Spread anormalement élargi" in msg


def test_format_daily_summary_message():
    summary = {
        "date": "2026-07-30", "nb_trades": 3, "nb_gagnants": 2, "nb_perdants": 1,
        "pnl_du_jour": 7.5, "solde_actuel": 107.5,
    }
    msg = tg.format_daily_summary_message(summary)
    assert "2026-07-30" in msg
    assert "107.50" in msg


def test_format_decision_message_dispatches_correctly():
    for decision_type, expected_word in [
        ("ENTER", "ENTRÉE"), ("EXIT", "SORTIE"), ("REDUCE", "RÉDUCTION"), ("HOLD", "HOLD"),
    ]:
        decision = {"decision": decision_type, "raisonnement": "test", "direction": "BUY"}
        msg = tg.format_decision_message(decision)
        assert expected_word in msg


def test_format_decision_message_unknown_type_raises():
    with pytest.raises(ValueError):
        tg.format_decision_message({"decision": "UNKNOWN"})


# --- Configuration ---

def test_get_config_raises_when_missing(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    with pytest.raises(tg.TelegramConfigError):
        tg.get_config()


def test_get_config_reads_env_vars(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    cfg = tg.get_config()
    assert cfg["token"] == "fake-token"
    assert cfg["chat_id"] == "12345"


def test_notify_hold_enabled_default_false(monkeypatch):
    monkeypatch.delenv("TELEGRAM_NOTIFY_HOLD", raising=False)
    assert tg.notify_hold_enabled() is False


def test_notify_hold_enabled_true(monkeypatch):
    monkeypatch.setenv("TELEGRAM_NOTIFY_HOLD", "true")
    assert tg.notify_hold_enabled() is True


# --- Envoi avec transport mocké (aucun réseau réel) ---

def make_mock_client(status_code=200, response_json=None):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=response_json or {"ok": True})
    transport = httpx.MockTransport(handler)
    return httpx.Client(transport=transport)


def test_send_message_success_with_mock_transport():
    client = make_mock_client(status_code=200)
    config = {"token": "fake-token", "chat_id": "12345"}
    result = tg.send_message("Test message", config=config, client=client)
    assert result is True


def test_send_message_failure_on_non_200():
    client = make_mock_client(status_code=400, response_json={"ok": False, "description": "Bad Request"})
    config = {"token": "fake-token", "chat_id": "12345"}
    result = tg.send_message("Test message", config=config, client=client)
    assert result is False


def test_send_message_missing_config_returns_false(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    result = tg.send_message("Test message")  # pas de config passée, lira l'env (absent)
    assert result is False


def test_send_message_network_error_returns_false():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connexion refusée (simulation)")
    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)
    config = {"token": "fake-token", "chat_id": "12345"}
    result = tg.send_message("Test message", config=config, client=client)
    assert result is False


def test_notify_decision_sends_enter():
    client = make_mock_client(status_code=200)
    config = {"token": "fake-token", "chat_id": "12345"}
    decision = {
        "decision": "ENTER", "direction": "BUY", "entry": 2000.0, "sl": 1995.0, "tp": 2010.0,
        "risque_dollars": 5.0, "rr_vise": 2.0, "scd": 2, "irv": 1.05, "fqe_score": 4,
        "raisonnement": "test",
    }
    result = tg.notify_decision(decision, config=config, client=client)
    assert result is True


def test_notify_decision_skips_hold_by_default(monkeypatch):
    monkeypatch.delenv("TELEGRAM_NOTIFY_HOLD", raising=False)
    client = make_mock_client(status_code=200)
    config = {"token": "fake-token", "chat_id": "12345"}
    decision = {"decision": "HOLD", "scd": 0, "irv": 1.0, "fqe_score": None, "raisonnement": "test"}
    result = tg.notify_decision(decision, config=config, client=client)
    assert result is False  # ignoré, pas d'envoi


def test_notify_decision_sends_hold_when_enabled(monkeypatch):
    monkeypatch.setenv("TELEGRAM_NOTIFY_HOLD", "true")
    client = make_mock_client(status_code=200)
    config = {"token": "fake-token", "chat_id": "12345"}
    decision = {"decision": "HOLD", "scd": 0, "irv": 1.0, "fqe_score": None, "raisonnement": "test"}
    result = tg.notify_decision(decision, config=config, client=client)
    assert result is True


def test_notify_urgent_alert_sends_message():
    client = make_mock_client(status_code=200)
    config = {"token": "fake-token", "chat_id": "12345"}
    result = tg.notify_urgent_alert("Test alerte urgente", config=config, client=client)
    assert result is True


def test_notify_daily_summary_sends_message():
    client = make_mock_client(status_code=200)
    config = {"token": "fake-token", "chat_id": "12345"}
    summary = {
        "date": "2026-07-30", "nb_trades": 3, "nb_gagnants": 2, "nb_perdants": 1,
        "pnl_du_jour": 7.5, "solde_actuel": 107.5,
    }
    result = tg.notify_daily_summary(summary, config=config, client=client)
    assert result is True