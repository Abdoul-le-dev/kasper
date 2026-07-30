"""
Tests d'intégration de l'API HTTP (endpoint /analyze), en isolation via
TestClient (aucun serveur réel n'est démarré, aucun réseau utilisé).
"""

import sys
import os
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi.testclient import TestClient
from trading_engine.api import app

client = TestClient(app)


def make_candle(o, h, l, c):
    return {"open": o, "high": h, "low": l, "close": c}


def build_flat_candles(n, base=2000.0):
    return [make_candle(base, base + 0.5, base - 0.5, base) for _ in range(n)]


def minimal_valid_payload():
    return {
        "timestamp": "2026-07-30T10:00:00Z",
        "compte": {
            "solde": 100.0,
            "equite": 100.0,
            "marge_utilisee": 0.0,
            "marge_disponible": 100.0,
            "positions_ouvertes": [],
        },
        "prix": {"actuel": 2000.0, "bid": 1999.9, "ask": 2000.1, "spread": 0.2},
        "D1": {"ohlc": build_flat_candles(60)},
        "H4": {"ohlc": build_flat_candles(220)},
        "H1": {"ohlc": build_flat_candles(60)},
        "M15": {"ohlc": build_flat_candles(30)},
        "M5": {"ohlc": build_flat_candles(30)},
        "evenements_macro_a_venir": [],
        "declencheur_alerte": "cyclique_30min",
        "perte_du_jour_cumulee": 0.0,
        "nombre_trades_perdants_jour": 0,
    }


def test_health_endpoint():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_risk_parameters_endpoint_matches_spec():
    response = client.get("/risk-parameters")
    assert response.status_code == 200
    data = response.json()
    assert data["risque_min_par_trade"] == 5.0
    assert data["perte_max_journaliere"] == 25.0
    assert data["positions_max_simultanees"] == 2
    assert data["rr_minimum"] == 2.0
    assert data["trades_perdants_max_par_jour"] == 5


def test_analyze_endpoint_returns_valid_decision_on_flat_market():
    payload = minimal_valid_payload()
    response = client.post("/analyze", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert data["decision"] in ("HOLD", "ENTER", "EXIT", "REDUCE")
    assert "timestamp_analyse" in data
    assert "raisonnement" in data


def test_analyze_endpoint_rejects_malformed_payload():
    bad_payload = {"timestamp": "2026-07-30T10:00:00Z"}  # champs requis manquants
    response = client.post("/analyze", json=bad_payload)
    assert response.status_code == 422  # erreur de validation Pydantic


def test_analyze_endpoint_rejects_missing_ohlc():
    payload = minimal_valid_payload()
    del payload["H1"]["ohlc"]
    response = client.post("/analyze", json=payload)
    assert response.status_code == 422


def test_analyze_endpoint_handles_open_position_payload():
    payload = minimal_valid_payload()
    payload["compte"]["positions_ouvertes"] = [{
        "direction": "BUY",
        "entry": 1995.0,
        "sl": 1990.0,
        "tp": 2005.0,
        "zone_reference_price": 1988.0,
        "partial_exit_taken": False,
    }]
    response = client.post("/analyze", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert data["decision"] in ("HOLD", "EXIT", "REDUCE")


def test_analyze_endpoint_with_macro_event_near():
    payload = minimal_valid_payload()
    payload["compte"]["positions_ouvertes"] = [{
        "direction": "BUY",
        "entry": 1995.0,
        "sl": 1990.0,
        "tp": 2005.0,
        "zone_reference_price": 1988.0,
        "partial_exit_taken": False,
    }]
    payload["evenements_macro_a_venir"] = [{"nom": "CPI", "impact": "high", "minutes_avant": 10}]
    response = client.post("/analyze", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert data["decision"] == "EXIT"
    assert "macro" in data["raisonnement"].lower()


def test_analyze_endpoint_rejects_invalid_macro_impact_type_gracefully():
    payload = minimal_valid_payload()
    payload["evenements_macro_a_venir"] = [{"impact": "high", "minutes_avant": "not_a_number"}]
    response = client.post("/analyze", json=payload)
    assert response.status_code == 422


def test_analyze_endpoint_never_fails_when_telegram_not_configured(monkeypatch):
    """
    Point critique: si TELEGRAM_BOT_TOKEN/CHAT_ID ne sont pas configurés (ou
    si Telegram est down), /analyze doit quand même retourner 200 — la
    notification est un effet secondaire, jamais un bloqueur de décision.
    """
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    payload = minimal_valid_payload()
    response = client.post("/analyze", json=payload)
    assert response.status_code == 200


def test_notify_urgent_alert_endpoint_returns_sent_status(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    response = client.post("/notify/urgent-alert", json={"reason": "Spread anormal détecté"})
    assert response.status_code == 200
    # Sans config Telegram valide, l'envoi échoue proprement (sent=False) mais l'endpoint répond 200
    assert response.json()["sent"] is False


def test_notify_daily_summary_endpoint_returns_sent_status(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    response = client.post("/notify/daily-summary", json={
        "date": "2026-07-30", "nb_trades": 2, "nb_gagnants": 1, "nb_perdants": 1,
        "pnl_du_jour": -2.5, "solde_actuel": 97.5,
    })
    assert response.status_code == 200
    assert response.json()["sent"] is False


def test_notify_urgent_alert_endpoint_rejects_missing_reason():
    response = client.post("/notify/urgent-alert", json={})
    assert response.status_code == 422
    