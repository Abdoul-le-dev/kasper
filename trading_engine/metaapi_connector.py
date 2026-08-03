"""
metaapi_connector.py

Connecteur vers l'API REST MetaApi.cloud, qui wrapper un compte MT5 (chez
n'importe quel broker MT5, ex: XM). Remplace oanda_connector.py.

Conserve exactement la même signature publique que oanda_connector — l'orchestrateur
n'a besoin de changer que l'import.

Configuration (variables d'environnement) :
    METAAPI_TOKEN           : token API généré depuis app.metaapi.cloud
    METAAPI_ACCOUNT_ID      : ID du compte MT5 déployé sur MetaApi (UUID)
    METAAPI_REGION          : région du datacenter (défaut: "new-york", autres: "london", "singapore")
    METAAPI_GENERATION      : "g1" (défaut, ancienne infra) ou "g2" (nouvelle infra cloud-g2)
    METAAPI_SYMBOL          : symbole XAUUSD selon le broker (XM utilise "GOLD" ou "XAUUSD"
                              selon le type de compte — à vérifier dans MT5)

Comment savoir si ton compte est g1 ou g2 :
- Ouvre https://app.metaapi.cloud et regarde le badge de ton compte MT5.
- Si tu vois "cloud-g2" affiché → METAAPI_GENERATION=g2
- Sinon (ancien compte, badge "cloud") → METAAPI_GENERATION=g1
- Les hostnames g2 contiennent ".g2." dans le domaine.

Architecture MetaApi (important à comprendre) :
- Client API (compte/positions/ordres)   : mt-client-api-v1.<region>[.g2].agiliumtrade.ai
- Market Data API (candles historiques) : mt-market-data-client-api-v1.<region>[.g2].agiliumtrade.ai
Deux hostnames différents, même token d'authentification (header `auth-token`).
"""

import os
import logging
from typing import Dict, List, Optional, Any

import httpx

logger = logging.getLogger("trading_engine.metaapi_connector")

# --- Hostnames selon la génération du compte MetaApi ---
CLIENT_API_HOST_TEMPLATE_G1 = "https://mt-client-api-v1.{region}.agiliumtrade.ai"
CLIENT_API_HOST_TEMPLATE_G2 = "https://mt-client-api-v1.{region}.g2.agiliumtrade.ai"
MARKET_DATA_HOST_TEMPLATE_G1 = "https://mt-market-data-client-api-v1.{region}.agiliumtrade.ai"
MARKET_DATA_HOST_TEMPLATE_G2 = "https://mt-market-data-client-api-v1.{region}.g2.agiliumtrade.ai"

DEFAULT_REGION = "new-york"
DEFAULT_GENERATION = "g1"
DEFAULT_SYMBOL = "XAUUSD"

# MetaApi utilise les codes MT5 natifs pour les timeframes
GRANULARITY_MAP = {
    "D1": "1d",
    "H4": "4h",
    "H1": "1h",
    "M15": "15m",
    "M5": "5m",
}


class MetaApiConfigError(Exception):
    pass


class MetaApiError(Exception):
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        self.message = message
        super().__init__(f"MetaApi error {status_code}: {message}")


def get_config() -> Dict[str, str]:
    token = os.environ.get("METAAPI_TOKEN")
    account_id = os.environ.get("METAAPI_ACCOUNT_ID")
    region = os.environ.get("METAAPI_REGION", DEFAULT_REGION)
    symbol = os.environ.get("METAAPI_SYMBOL", DEFAULT_SYMBOL)
    generation = os.environ.get("METAAPI_GENERATION", DEFAULT_GENERATION)

    if not token or not account_id:
        raise MetaApiConfigError(
            "METAAPI_TOKEN et METAAPI_ACCOUNT_ID doivent être définis en variables d'environnement"
        )
    if generation not in ("g1", "g2"):
        raise MetaApiConfigError(
            f"METAAPI_GENERATION doit être 'g1' ou 'g2', reçu: {generation}"
        )
    return {
        "token": token,
        "account_id": account_id,
        "region": region,
        "symbol": symbol,
        "generation": generation,
    }


def _client_host(config: Dict[str, str]) -> str:
    template = (
        CLIENT_API_HOST_TEMPLATE_G2
        if config.get("generation") == "g2"
        else CLIENT_API_HOST_TEMPLATE_G1
    )
    return template.format(region=config["region"])


def _market_data_host(config: Dict[str, str]) -> str:
    template = (
        MARKET_DATA_HOST_TEMPLATE_G2
        if config.get("generation") == "g2"
        else MARKET_DATA_HOST_TEMPLATE_G1
    )
    return template.format(region=config["region"])


def _headers(config: Dict[str, str]) -> Dict[str, str]:
    return {"auth-token": config["token"], "Content-Type": "application/json"}


def _request(
    method: str,
    url: str,
    config: Dict[str, str],
    client: Optional[httpx.Client] = None,
    params: Optional[Dict[str, Any]] = None,
    json_body: Optional[Dict[str, Any]] = None,
) -> Any:
    """Effectue une requête HTTP vers MetaApi et lève MetaApiError si non-2xx."""
    owns_client = client is None
    http_client = client or httpx.Client(timeout=30.0)
    try:
        response = http_client.request(
            method, url, headers=_headers(config), params=params, json=json_body
        )
        if response.status_code not in (200, 201, 204):
            raise MetaApiError(response.status_code, response.text)
        if response.status_code == 204:
            return {}
        return response.json()
    finally:
        if owns_client:
            http_client.close()


# --- Données de marché ---

def _parse_candle(raw: Dict) -> Dict[str, float]:
    """Convertit une bougie MetaApi au format attendu par le moteur (open/high/low/close)."""
    return {
        "open": float(raw["open"]),
        "high": float(raw["high"]),
        "low": float(raw["low"]),
        "close": float(raw["close"]),
    }


def get_candles(
    timeframe: str,
    count: int = 250,
    instrument: Optional[str] = None,
    config: Optional[Dict[str, str]] = None,
    client: Optional[httpx.Client] = None,
) -> List[Dict[str, float]]:
    """
    Récupère les bougies OHLC historiques via l'endpoint historical-market-data,
    ordonnées de la plus ancienne à la plus récente.

    Endpoint: GET /users/current/accounts/:accountId/historical-market-data/
              symbols/:symbol/timeframes/:timeframe/candles?limit=<count>
    """
    if timeframe not in GRANULARITY_MAP:
        raise ValueError(f"Timeframe inconnu: {timeframe}. Attendu: {list(GRANULARITY_MAP)}")

    cfg = config or get_config()
    symbol = instrument or cfg["symbol"]
    tf = GRANULARITY_MAP[timeframe]

    url = (
        f"{_market_data_host(cfg)}/users/current/accounts/{cfg['account_id']}"
        f"/historical-market-data/symbols/{symbol}/timeframes/{tf}/candles"
    )
    params = {"limit": count}

    data = _request("GET", url, cfg, client=client, params=params)
    # L'API retourne une liste de bougies, potentiellement dans l'ordre récent -> ancien
    # selon le broker/version. On force le tri chronologique par 'time' pour être robustes.
    candles = sorted(data, key=lambda c: c.get("time", ""))
    return [_parse_candle(c) for c in candles]


def get_pricing(
    instrument: Optional[str] = None,
    config: Optional[Dict[str, str]] = None,
    client: Optional[httpx.Client] = None,
) -> Dict[str, float]:
    """
    Récupère le prix actuel via l'endpoint current-price.
    Endpoint: GET /users/current/accounts/:accountId/symbols/:symbol/current-price
    """
    cfg = config or get_config()
    symbol = instrument or cfg["symbol"]
    url = f"{_client_host(cfg)}/users/current/accounts/{cfg['account_id']}/symbols/{symbol}/current-price"

    data = _request("GET", url, cfg, client=client)
    bid = float(data["bid"])
    ask = float(data["ask"])
    mid = round((bid + ask) / 2, 5)
    spread = round(ask - bid, 5)
    return {"bid": bid, "ask": ask, "actuel": mid, "spread": spread}


# --- Compte et positions ---

def get_account_summary(
    config: Optional[Dict[str, str]] = None,
    client: Optional[httpx.Client] = None,
) -> Dict[str, float]:
    """
    Récupère les infos du compte.
    Endpoint: GET /users/current/accounts/:accountId/account-information
    """
    cfg = config or get_config()
    url = f"{_client_host(cfg)}/users/current/accounts/{cfg['account_id']}/account-information"

    data = _request("GET", url, cfg, client=client)
    return {
        "solde": float(data["balance"]),
        "equite": float(data["equity"]),
        "marge_utilisee": float(data.get("margin", 0.0)),
        "marge_disponible": float(data.get("freeMargin", 0.0)),
    }


def get_open_trades(
    instrument: Optional[str] = None,
    config: Optional[Dict[str, str]] = None,
    client: Optional[httpx.Client] = None,
) -> List[Dict[str, Any]]:
    """
    Récupère les positions ouvertes filtrées sur l'instrument.
    Endpoint: GET /users/current/accounts/:accountId/positions
    """
    cfg = config or get_config()
    symbol = instrument or cfg["symbol"]
    url = f"{_client_host(cfg)}/users/current/accounts/{cfg['account_id']}/positions"

    data = _request("GET", url, cfg, client=client)
    trades = []
    for p in data:
        if p.get("symbol") != symbol:
            continue
        direction = "BUY" if p.get("type") == "POSITION_TYPE_BUY" else "SELL"
        trades.append({
            "trade_id": str(p["id"]),
            "direction": direction,
            "entry": float(p["openPrice"]),
            "units": float(p["volume"]),  # en lots MT5 (pas en unités OANDA)
            "sl": float(p["stopLoss"]) if p.get("stopLoss") else None,
            "tp": float(p["takeProfit"]) if p.get("takeProfit") else None,
            "profit": float(p.get("profit", 0.0)),  # P&L flottant actuel
        })
    return trades


def get_position_by_id(
    trade_id: str,
    config: Optional[Dict[str, str]] = None,
    client: Optional[httpx.Client] = None,
) -> Dict[str, Any]:
    """
    Récupère les détails d'une position spécifique (incluant le P&L courant).
    Endpoint: GET /users/current/accounts/:accountId/positions/:positionId
    """
    cfg = config or get_config()
    url = f"{_client_host(cfg)}/users/current/accounts/{cfg['account_id']}/positions/{trade_id}"
    return _request("GET", url, cfg, client=client)


# --- Sizing ---

def calculate_units(risk_dollars: float, distance_price: float, direction: str, lot_size: float = 100.0) -> float:
    """
    Calcule le volume en LOTS MT5 pour XAUUSD.

    Sur XAUUSD chez la plupart des brokers MT5 (dont XM):
    - 1 lot standard = 100 onces d'or
    - Valeur du pip: dépend de la définition du pip par le broker (souvent 0.01 = 1$/lot,
      ou 0.1 = 1$/lot selon le compte)
    - On simplifie: pour XAUUSD, 1 lot * mouvement de 1$ = 100$ de P&L
      → volume_lots = risque_$ / (distance_prix * lot_size)

    Ex: risque 5$, SL à 5$ de distance, lot_size=100 → volume = 5 / (5 * 100) = 0.01 lot (minimum courant)

    Arrondi à 2 décimales (0.01 = volume minimum MT5 standard).
    Retourne un float positif — le sens BUY/SELL est passé séparément à place_market_order.
    """
    if distance_price <= 0:
        raise ValueError("distance_price doit être > 0")
    if lot_size <= 0:
        raise ValueError("lot_size doit être > 0")

    raw_volume = risk_dollars / (distance_price * lot_size)
    volume = round(raw_volume, 2)
    # Volume minimum standard sur XAUUSD chez XM: 0.01 lot (micro-lot)
    return max(volume, 0.01)


# --- Exécution des ordres ---

def place_market_order(
    direction: str,
    units: float,
    sl: float,
    tp: float,
    instrument: Optional[str] = None,
    config: Optional[Dict[str, str]] = None,
    client: Optional[httpx.Client] = None,
) -> Dict[str, Any]:
    """
    Place un ordre au marché avec SL/TP attachés.
    Endpoint: POST /users/current/accounts/:accountId/trade
    Corps: {"actionType": "ORDER_TYPE_BUY"|"ORDER_TYPE_SELL", "symbol", "volume", "stopLoss", "takeProfit"}

    `units` doit être un volume positif en lots (0.01 minimum). Le sens est
    encodé dans `actionType`.
    """
    cfg = config or get_config()
    symbol = instrument or cfg["symbol"]

    action_type = "ORDER_TYPE_BUY" if direction == "BUY" else "ORDER_TYPE_SELL"
    body = {
        "actionType": action_type,
        "symbol": symbol,
        "volume": round(abs(units), 2),
        "stopLoss": round(sl, 2),
        "takeProfit": round(tp, 2),
    }

    url = f"{_client_host(cfg)}/users/current/accounts/{cfg['account_id']}/trade"
    return _request("POST", url, cfg, client=client, json_body=body)


def close_trade(
    trade_id: str,
    units: Optional[float] = None,
    config: Optional[Dict[str, str]] = None,
    client: Optional[httpx.Client] = None,
) -> Dict[str, Any]:
    """
    Ferme une position, totalement ou partiellement.

    Fermeture totale:
        Corps: {"actionType": "POSITION_CLOSE_ID", "positionId": <id>}

    Fermeture partielle:
        Corps: {"actionType": "POSITION_PARTIAL", "positionId": <id>, "volume": <volume à fermer>}
    """
    cfg = config or get_config()
    if units is None:
        body = {"actionType": "POSITION_CLOSE_ID", "positionId": trade_id}
    else:
        body = {
            "actionType": "POSITION_PARTIAL",
            "positionId": trade_id,
            "volume": round(abs(units), 2),
        }
    url = f"{_client_host(cfg)}/users/current/accounts/{cfg['account_id']}/trade"
    return _request("POST", url, cfg, client=client, json_body=body)


def set_trade_stop_loss(
    trade_id: str,
    new_sl: float,
    config: Optional[Dict[str, str]] = None,
    client: Optional[httpx.Client] = None,
) -> Dict[str, Any]:
    """
    Modifie le SL d'une position ouverte (utilisé pour remonter le SL au breakeven après 1R).
    Corps: {"actionType": "POSITION_MODIFY", "positionId": <id>, "stopLoss": <nouveau SL>}
    """
    cfg = config or get_config()
    body = {
        "actionType": "POSITION_MODIFY",
        "positionId": trade_id,
        "stopLoss": round(new_sl, 2),
    }
    url = f"{_client_host(cfg)}/users/current/accounts/{cfg['account_id']}/trade"
    return _request("POST", url, cfg, client=client, json_body=body)