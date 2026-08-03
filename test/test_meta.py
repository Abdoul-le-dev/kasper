"""
Tests isolés du module metaapi_connector.py, via httpx.MockTransport.
Aucune requête réseau réelle n'est faite vers l'API MetaApi.
"""

import sys
import os
import json
import pytest
import httpx

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from trading_engine import metaapi_connector as meta


CONFIG = {
    "token": "fake-token",
    "account_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
    "region": "new-york",
    "symbol": "XAUUSD",
}


def mock_client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


# --- Configuration ---

def test_get_config_raises_when_missing(monkeypatch):
    monkeypatch.delenv("METAAPI_TOKEN", raising=False)
    monkeypatch.delenv("METAAPI_ACCOUNT_ID", raising=False)
    with pytest.raises(meta.MetaApiConfigError):
        meta.get_config()


def test_get_config_reads_env(monkeypatch):
    monkeypatch.setenv("METAAPI_TOKEN", "tok")
    monkeypatch.setenv("METAAPI_ACCOUNT_ID", "abc-123")
    monkeypatch.setenv("METAAPI_REGION", "london")
    monkeypatch.setenv("METAAPI_SYMBOL", "GOLD")
    cfg = meta.get_config()
    assert cfg["token"] == "tok"
    assert cfg["region"] == "london"
    assert cfg["symbol"] == "GOLD"


def test_get_config_uses_defaults(monkeypatch):
    monkeypatch.setenv("METAAPI_TOKEN", "tok")
    monkeypatch.setenv("METAAPI_ACCOUNT_ID", "abc-123")
    monkeypatch.delenv("METAAPI_REGION", raising=False)
    monkeypatch.delenv("METAAPI_SYMBOL", raising=False)
    cfg = meta.get_config()
    assert cfg["region"] == "new-york"
    assert cfg["symbol"] == "XAUUSD"


# --- Candles ---

def make_candle_response(n=5):
    candles = []
    price = 2000.0
    for i in range(n):
        candles.append({
            "symbol": "XAUUSD",
            "timeframe": "15m",
            "time": f"2026-07-30T{10+i:02d}:00:00.000Z",
            "brokerTime": f"2026-07-30 {10+i:02d}:00:00.000",
            "open": price,
            "high": price + 1,
            "low": price - 1,
            "close": price + 0.5,
            "tickVolume": 1000,
            "spread": 20,
            "volume": 500,
        })
        price += 1
    return candles


def test_get_candles_parses_response_correctly():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "historical-market-data" in str(request.url)
        assert "XAUUSD" in str(request.url)
        assert "15m" in str(request.url)
        assert "mt-market-data-client-api-v1.new-york.agiliumtrade.ai" in str(request.url)
        return httpx.Response(200, json=make_candle_response(5))

    client = mock_client(handler)
    candles = meta.get_candles("M15", count=5, config=CONFIG, client=client)
    assert len(candles) == 5
    assert set(candles[0].keys()) == {"open", "high", "low", "close"}
    assert candles[0]["open"] == 2000.0


def test_get_candles_sorts_chronologically():
    """Si l'API renvoie les bougies dans le désordre, le connecteur doit les trier."""
    response = make_candle_response(3)
    shuffled = [response[2], response[0], response[1]]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=shuffled)

    client = mock_client(handler)
    candles = meta.get_candles("M15", count=3, config=CONFIG, client=client)
    assert candles[0]["open"] == 2000.0  # première dans l'ordre chronologique
    assert candles[-1]["open"] == 2002.0


def test_get_candles_invalid_timeframe_raises():
    with pytest.raises(ValueError):
        meta.get_candles("M1", config=CONFIG, client=mock_client(lambda r: httpx.Response(200, json=[])))


def test_get_candles_error_response_raises_metaapi_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text='{"error": "Unauthorized"}')

    client = mock_client(handler)
    with pytest.raises(meta.MetaApiError):
        meta.get_candles("M15", config=CONFIG, client=client)


# --- Pricing ---

def test_get_pricing_computes_mid_and_spread():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "current-price" in str(request.url)
        return httpx.Response(200, json={
            "symbol": "XAUUSD", "bid": 1999.90, "ask": 2000.10,
            "time": "2026-07-30T10:00:00.000Z",
        })

    client = mock_client(handler)
    pricing = meta.get_pricing(config=CONFIG, client=client)
    assert pricing["bid"] == 1999.90
    assert pricing["ask"] == 2000.10
    assert pricing["actuel"] == pytest.approx(2000.0)
    assert pricing["spread"] == pytest.approx(0.20)


# --- Account summary ---

def test_get_account_summary_parses_fields():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "account-information" in str(request.url)
        return httpx.Response(200, json={
            "broker": "XM", "currency": "USD",
            "balance": 100.0, "equity": 102.5, "margin": 10.0, "freeMargin": 92.5,
            "leverage": 500, "marginLevel": 1025.0,
        })

    client = mock_client(handler)
    account = meta.get_account_summary(config=CONFIG, client=client)
    assert account["solde"] == 100.0
    assert account["equite"] == 102.5
    assert account["marge_utilisee"] == 10.0
    assert account["marge_disponible"] == 92.5


# --- Open trades ---

def test_get_open_trades_parses_buy_and_sell():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[
            {
                "id": "46648037", "type": "POSITION_TYPE_BUY", "symbol": "XAUUSD",
                "openPrice": 2000.0, "volume": 0.01,
                "stopLoss": 1995.0, "takeProfit": 2010.0,
                "profit": 5.5,
            },
            {
                "id": "46648038", "type": "POSITION_TYPE_SELL", "symbol": "XAUUSD",
                "openPrice": 2005.0, "volume": 0.02,
                "profit": -2.0,
            },
            {
                "id": "46648039", "type": "POSITION_TYPE_BUY", "symbol": "EURUSD",
                "openPrice": 1.1, "volume": 0.1,
                "profit": 0.0,
            },
        ])

    client = mock_client(handler)
    trades = meta.get_open_trades(instrument="XAUUSD", config=CONFIG, client=client)
    assert len(trades) == 2  # EURUSD filtré
    assert trades[0]["direction"] == "BUY"
    assert trades[0]["sl"] == 1995.0
    assert trades[0]["tp"] == 2010.0
    assert trades[0]["profit"] == 5.5
    assert trades[1]["direction"] == "SELL"
    assert trades[1]["sl"] is None
    assert trades[1]["profit"] == -2.0


def test_get_position_by_id_returns_details():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "/positions/46648037" in str(request.url)
        return httpx.Response(200, json={
            "id": "46648037", "profit": 7.5, "symbol": "XAUUSD", "volume": 0.01,
        })

    client = mock_client(handler)
    detail = meta.get_position_by_id("46648037", config=CONFIG, client=client)
    assert detail["profit"] == 7.5


# --- Calcul des unités (lots) ---

def test_calculate_units_returns_minimum_lot():
    volume = meta.calculate_units(risk_dollars=5.0, distance_price=5.0, direction="BUY")
    # 5 / (5 * 100) = 0.01 → volume minimum
    assert volume == 0.01


def test_calculate_units_larger_risk_larger_volume():
    volume = meta.calculate_units(risk_dollars=50.0, distance_price=5.0, direction="BUY")
    # 50 / (5 * 100) = 0.1
    assert volume == pytest.approx(0.10)


def test_calculate_units_smaller_distance_larger_volume():
    volume = meta.calculate_units(risk_dollars=5.0, distance_price=1.0, direction="BUY")
    # 5 / (1 * 100) = 0.05
    assert volume == pytest.approx(0.05)


def test_calculate_units_invalid_distance_raises():
    with pytest.raises(ValueError):
        meta.calculate_units(risk_dollars=5.0, distance_price=0, direction="BUY")


def test_calculate_units_invalid_lot_size_raises():
    with pytest.raises(ValueError):
        meta.calculate_units(risk_dollars=5.0, distance_price=5.0, direction="BUY", lot_size=0)


def test_calculate_units_never_below_minimum():
    volume = meta.calculate_units(risk_dollars=1.0, distance_price=100.0, direction="BUY")
    # 1 / (100 * 100) = 0.0001 → forcé à 0.01
    assert volume == 0.01


# --- Ordres ---

def test_place_market_order_sends_correct_body():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"positionId": "999", "orderId": "888"})

    client = mock_client(handler)
    result = meta.place_market_order("BUY", 0.01, sl=1995.0, tp=2010.0, config=CONFIG, client=client)

    assert captured["body"]["actionType"] == "ORDER_TYPE_BUY"
    assert captured["body"]["symbol"] == "XAUUSD"
    assert captured["body"]["volume"] == 0.01
    assert captured["body"]["stopLoss"] == 1995.0
    assert captured["body"]["takeProfit"] == 2010.0
    assert result["positionId"] == "999"


def test_place_market_order_sell_uses_correct_action_type():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={})

    client = mock_client(handler)
    meta.place_market_order("SELL", 0.02, sl=2005.0, tp=1990.0, config=CONFIG, client=client)
    assert captured["body"]["actionType"] == "ORDER_TYPE_SELL"
    assert captured["body"]["volume"] == 0.02


def test_close_trade_all_volume():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={})

    client = mock_client(handler)
    meta.close_trade("101", config=CONFIG, client=client)
    assert captured["body"]["actionType"] == "POSITION_CLOSE_ID"
    assert captured["body"]["positionId"] == "101"
    assert "volume" not in captured["body"]


def test_close_trade_partial_volume():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={})

    client = mock_client(handler)
    meta.close_trade("101", units=0.005, config=CONFIG, client=client)
    assert captured["body"]["actionType"] == "POSITION_PARTIAL"
    assert captured["body"]["volume"] == 0.01  # arrondi à 2 décimales


def test_set_trade_stop_loss_sends_correct_price():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={})

    client = mock_client(handler)
    meta.set_trade_stop_loss("101", 2000.0, config=CONFIG, client=client)
    assert captured["body"]["actionType"] == "POSITION_MODIFY"
    assert captured["body"]["positionId"] == "101"
    assert captured["body"]["stopLoss"] == 2000.0


def test_request_raises_metaapi_error_on_500():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="Internal Server Error")

    client = mock_client(handler)
    with pytest.raises(meta.MetaApiError) as exc_info:
        meta.get_account_summary(config=CONFIG, client=client)
    assert exc_info.value.status_code == 500


def test_request_handles_204_no_content():
    """Certains endpoints MetaApi retournent 204 No Content sur succès (ex: modification)."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(204)

    client = mock_client(handler)
    # Un 204 ne doit pas lever d'exception et retourne un dict vide
    result = meta.set_trade_stop_loss("101", 2000.0, config=CONFIG, client=client)
    assert result == {}


def test_hostnames_are_correctly_regional():
    """Test que les hostnames prennent bien en compte la région configurée."""
    london_config = {**CONFIG, "region": "london"}
    assert "london" in meta._client_host(london_config)
    assert "london" in meta._market_data_host(london_config)
    singapore_config = {**CONFIG, "region": "singapore"}
    assert "singapore" in meta._client_host(singapore_config)