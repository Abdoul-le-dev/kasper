"""
Tests isolés de orchestrator.py.

Stratégie de test : plutôt que de mocker le réseau HTTP à bas niveau pour
chaque dépendance, on mocke directement les fonctions de oanda_connector et
journal (déjà testées unitairement dans leurs propres fichiers de test) —
ce qui isole la LOGIQUE d'orchestration (quel appel fait quoi, dans quel
ordre, avec quels arguments) du détail d'implémentation réseau.

call_engine() est testé séparément avec un vrai transport HTTP mocké (httpx.MockTransport)
car c'est la seule fonction de ce module qui parle HTTP directement.
"""

import sys
import os
import pytest
import httpx

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from trading_engine import orchestrator as orch
from trading_engine import metaapi_connector as broker
from trading_engine import journal


FAKE_CONFIG = {"token": "t", "account_id": "101-001-1234-001", "environment": "practice"}


@pytest.fixture(autouse=True)
def reset_orchestrator_globals():
    """Réinitialise l'état module-level (historique de spread) entre les tests."""
    orch._SPREAD_HISTORY.clear()
    orch._last_price_seen = None
    yield
    orch._SPREAD_HISTORY.clear()
    orch._last_price_seen = None


# --- build_payload ---

def test_build_payload_assembles_expected_structure(monkeypatch):
    monkeypatch.setattr(broker, "get_config", lambda: FAKE_CONFIG)
    monkeypatch.setattr(broker, "get_account_summary", lambda **kw: {
        "solde": 100.0, "equite": 100.0, "marge_utilisee": 0.0, "marge_disponible": 100.0
    })
    monkeypatch.setattr(broker, "get_pricing", lambda **kw: {"bid": 1999.9, "ask": 2000.1, "actuel": 2000.0, "spread": 0.2})
    monkeypatch.setattr(broker, "get_candles", lambda tf, **kw: [{"open": 2000, "high": 2001, "low": 1999, "close": 2000.5}] * 5)
    monkeypatch.setattr(broker, "get_open_trades", lambda **kw: [])
    monkeypatch.setattr(journal, "get_daily_state", lambda date_str, **kw: {"perte_du_jour_cumulee": 3.0, "nombre_trades_perdants_jour": 1})

    payload = orch.build_payload(declencheur="cyclique_30min")

    assert payload["compte"]["solde"] == 100.0
    assert payload["prix"]["actuel"] == 2000.0
    assert payload["perte_du_jour_cumulee"] == 3.0
    assert payload["nombre_trades_perdants_jour"] == 1
    assert payload["declencheur_alerte"] == "cyclique_30min"
    assert "D1" in payload and "H4" in payload and "H1" in payload and "M15" in payload and "M5" in payload
    assert payload["compte"]["positions_ouvertes"] == []


def test_build_payload_enriches_open_trades_with_metadata(monkeypatch):
    monkeypatch.setattr(broker, "get_config", lambda: FAKE_CONFIG)
    monkeypatch.setattr(broker, "get_account_summary", lambda **kw: {"solde": 100.0, "equite": 100.0, "marge_utilisee": 5.0, "marge_disponible": 95.0})
    monkeypatch.setattr(broker, "get_pricing", lambda **kw: {"bid": 1999.9, "ask": 2000.1, "actuel": 2000.0, "spread": 0.2})
    monkeypatch.setattr(broker, "get_candles", lambda tf, **kw: [{"open": 2000, "high": 2001, "low": 1999, "close": 2000.5}] * 5)
    monkeypatch.setattr(broker, "get_open_trades", lambda **kw: [{
        "trade_id": "101", "direction": "BUY", "entry": 2000.0, "sl": 1995.0, "tp": 2010.0, "units": 1.0
    }])
    monkeypatch.setattr(journal, "get_position_metadata", lambda trade_id, **kw: {"zone_reference_price": 1994.0, "partial_exit_taken": True})
    monkeypatch.setattr(journal, "get_daily_state", lambda date_str, **kw: {"perte_du_jour_cumulee": 0.0, "nombre_trades_perdants_jour": 0})

    payload = orch.build_payload()
    position = payload["compte"]["positions_ouvertes"][0]
    assert position["zone_reference_price"] == 1994.0
    assert position["partial_exit_taken"] is True


# --- call_engine ---

def test_call_engine_posts_to_analyze_and_returns_json():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/analyze"
        return httpx.Response(200, json={"decision": "HOLD", "raisonnement": "test"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = orch.call_engine({"some": "payload"}, client=client)
    assert result["decision"] == "HOLD"


def test_call_engine_raises_on_http_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="Internal Error")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(httpx.HTTPStatusError):
        orch.call_engine({"some": "payload"}, client=client)


# --- execute_decision ---

def test_execute_decision_enter_places_order_and_saves_metadata(monkeypatch):
    monkeypatch.setattr(broker, "get_config", lambda: FAKE_CONFIG)
    monkeypatch.setattr(broker, "calculate_units", lambda risk, dist, direction: 0.01 if direction == "BUY" else 0.01)

    captured = {}
    def fake_place_order(direction, units, sl, tp, **kw):
        captured["direction"] = direction
        captured["units"] = units
        captured["sl"] = sl
        captured["tp"] = tp
        # MetaApi retourne positionId au niveau racine
        return {"positionId": "999", "orderId": "888", "numericCode": 10009}
    monkeypatch.setattr(broker, "place_market_order", fake_place_order)

    saved_meta = {}
    monkeypatch.setattr(journal, "save_position_metadata", lambda trade_id, meta, **kw: saved_meta.update({trade_id: meta}))

    decision = {
        "decision": "ENTER", "direction": "BUY", "entry": 2000.0, "sl": 1995.0, "tp": 2010.0,
        "risque_dollars": 5.0, "zone_reference_price": 1994.0,
    }
    orch.execute_decision(decision, payload={})

    assert captured["direction"] == "BUY"
    assert captured["units"] == 0.01
    assert saved_meta["999"]["zone_reference_price"] == 1994.0
    assert saved_meta["999"]["partial_exit_taken"] is False


def test_execute_decision_enter_logs_warning_when_no_trade_id(monkeypatch, caplog):
    monkeypatch.setattr(broker, "get_config", lambda: FAKE_CONFIG)
    monkeypatch.setattr(broker, "calculate_units", lambda risk, dist, direction: 0.01)
    # Réponse sans positionId ni orderId (rejet MetaApi)
    monkeypatch.setattr(broker, "place_market_order", lambda *a, **kw: {"numericCode": 10004, "stringCode": "REQUOTE"})

    decision = {"decision": "ENTER", "direction": "BUY", "entry": 2000.0, "sl": 1995.0, "tp": 2010.0, "risque_dollars": 5.0}
    orch.execute_decision(decision, payload={})  # ne doit pas lever d'exception


def test_execute_decision_exit_closes_all_trades_and_updates_daily_state(monkeypatch):
    monkeypatch.setattr(broker, "get_config", lambda: FAKE_CONFIG)
    # MetaApi: get_open_trades retourne désormais 'profit' au niveau du trade
    monkeypatch.setattr(broker, "get_open_trades", lambda **kw: [{"trade_id": "101", "units": 0.01, "profit": -4.5}])
    monkeypatch.setattr(broker, "close_trade", lambda trade_id, **kw: {"numericCode": 10009})

    updated = {}
    monkeypatch.setattr(journal, "update_daily_state", lambda date_str, loss_delta, is_losing_trade, **kw: updated.update(
        {"loss_delta": loss_delta, "is_losing_trade": is_losing_trade}
    ))
    deleted = []
    monkeypatch.setattr(journal, "delete_position_metadata", lambda trade_id, **kw: deleted.append(trade_id))

    orch.execute_decision({"decision": "EXIT"}, payload={})

    assert updated["loss_delta"] == 4.5
    assert updated["is_losing_trade"] is True
    assert deleted == ["101"]


def test_execute_decision_exit_winning_trade_does_not_count_as_loss(monkeypatch):
    monkeypatch.setattr(broker, "get_config", lambda: FAKE_CONFIG)
    monkeypatch.setattr(broker, "get_open_trades", lambda **kw: [{"trade_id": "101", "units": 0.01, "profit": 7.0}])
    monkeypatch.setattr(broker, "close_trade", lambda trade_id, **kw: {"numericCode": 10009})

    updated = {}
    monkeypatch.setattr(journal, "update_daily_state", lambda date_str, loss_delta, is_losing_trade, **kw: updated.update(
        {"loss_delta": loss_delta, "is_losing_trade": is_losing_trade}
    ))
    monkeypatch.setattr(journal, "delete_position_metadata", lambda trade_id, **kw: None)

    orch.execute_decision({"decision": "EXIT"}, payload={})

    assert updated["loss_delta"] == 0.0
    assert updated["is_losing_trade"] is False


def test_execute_decision_reduce_partial_closes_and_moves_sl(monkeypatch):
    monkeypatch.setattr(broker, "get_config", lambda: FAKE_CONFIG)
    # units en lots MT5 (0.02 lots ouverts, on ferme 50% = 0.01)
    monkeypatch.setattr(broker, "get_open_trades", lambda **kw: [{"trade_id": "101", "units": 0.02, "profit": 3.0}])

    closed = {}
    monkeypatch.setattr(broker, "close_trade", lambda trade_id, units=None, **kw: closed.update({"trade_id": trade_id, "units": units}))
    sl_moved = {}
    monkeypatch.setattr(broker, "set_trade_stop_loss", lambda trade_id, new_sl, **kw: sl_moved.update({"trade_id": trade_id, "new_sl": new_sl}))
    monkeypatch.setattr(journal, "get_position_metadata", lambda trade_id, **kw: {"zone_reference_price": 1994.0, "partial_exit_taken": False})
    saved = {}
    monkeypatch.setattr(journal, "save_position_metadata", lambda trade_id, meta, **kw: saved.update({trade_id: meta}))

    decision = {"decision": "REDUCE", "pourcentage_reduction": 50, "nouveau_sl": 2000.0}
    orch.execute_decision(decision, payload={})

    assert closed["units"] == 0.01  # 50% de 0.02 lot
    assert sl_moved["new_sl"] == 2000.0
    assert saved["101"]["partial_exit_taken"] is True


def test_execute_decision_hold_calls_nothing(monkeypatch):
    monkeypatch.setattr(broker, "get_config", lambda: FAKE_CONFIG)
    # Si HOLD appelle la moindre fonction oanda de trading, ces mocks lèveraient une erreur
    monkeypatch.setattr(broker, "place_market_order", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("ne devrait pas être appelé")))
    monkeypatch.setattr(broker, "close_trade", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("ne devrait pas être appelé")))

    orch.execute_decision({"decision": "HOLD"}, payload={})  # ne doit rien faire, ne doit pas lever


# --- run_cycle ---

def test_run_cycle_orchestrates_build_call_execute_log(monkeypatch):
    call_order = []

    monkeypatch.setattr(orch, "build_payload", lambda declencheur: call_order.append("build") or {"payload": True})
    monkeypatch.setattr(orch, "call_engine", lambda payload, client=None: call_order.append("call") or {"decision": "HOLD"})
    monkeypatch.setattr(orch, "execute_decision", lambda decision, payload, client=None: call_order.append("execute"))
    monkeypatch.setattr(journal, "log_decision", lambda payload, decision, **kw: call_order.append("log"))

    result = orch.run_cycle()

    assert call_order == ["build", "call", "execute", "log"]
    assert result["decision"] == "HOLD"


def test_run_cycle_notifies_urgent_alert_on_exception(monkeypatch):
    monkeypatch.setattr(orch, "build_payload", lambda declencheur: (_ for _ in ()).throw(RuntimeError("panne réseau broker")))

    alerted = []
    monkeypatch.setattr(orch.tg, "notify_urgent_alert", lambda reason, **kw: alerted.append(reason))

    with pytest.raises(RuntimeError):
        orch.run_cycle()

    assert len(alerted) == 1
    assert "erreur technique" in alerted[0].lower()


# --- monitor_urgent_conditions ---

def test_monitor_urgent_conditions_alerts_on_spread_spike(monkeypatch):
    prices = iter([{"bid": 2000.0, "ask": 2000.1, "actuel": 2000.05, "spread": 0.1}] * 20 +
                  [{"bid": 2000.0, "ask": 2001.0, "actuel": 2000.5, "spread": 1.0}])

    monkeypatch.setattr(broker, "get_config", lambda: FAKE_CONFIG)
    monkeypatch.setattr(broker, "get_pricing", lambda **kw: next(prices))

    alerted = []
    monkeypatch.setattr(orch.tg, "notify_urgent_alert", lambda reason, **kw: alerted.append(reason))

    for _ in range(21):
        orch.monitor_urgent_conditions()

    assert any("spread" in a.lower() for a in alerted)


def test_monitor_urgent_conditions_handles_oanda_error_gracefully(monkeypatch):
    monkeypatch.setattr(broker, "get_config", lambda: (_ for _ in ()).throw(broker.MetaApiConfigError("pas configuré")))
    orch.monitor_urgent_conditions()  # ne doit pas lever d'exception


def test_avg_spread_gap_threshold():
    assert orch.avg_spread_gap_threshold(5.0, threshold=3.0) is True
    assert orch.avg_spread_gap_threshold(1.0, threshold=3.0) is False