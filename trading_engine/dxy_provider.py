"""
dxy_provider.py

Récupère le DXY (Dollar Index) via Yahoo Finance (endpoint public v8/chart).
L'or est fortement inversement corrélé au DXY — c'est un contexte essentiel
pour Claude.

Pas de clé API requise, pas de rate limit strict pour usage modéré.
Endpoint : https://query1.finance.yahoo.com/v8/finance/chart/DX-Y.NYB

Testable via injection de client httpx (comme MetaApi et Claude).
"""

import logging
from typing import Dict, List, Optional

import httpx

logger = logging.getLogger("trading_engine.dxy_provider")

YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/DX-Y.NYB"
DEFAULT_TIMEOUT = 10.0


class DXYProviderError(Exception):
    pass


def get_dxy_context(
    range_period: str = "5d",
    interval: str = "1h",
    client: Optional[httpx.Client] = None,
) -> Dict:
    """
    Retourne un contexte DXY complet:
    - Prix actuel
    - Variation 24h (%)
    - Variation 7 jours (%)
    - Direction (haussière / baissière / neutre)
    - Dernières 24 valeurs H1 (pour que Claude voie le mouvement récent)

    En cas d'erreur (Yahoo down, réponse malformée), retourne un contexte
    "unavailable" plutôt que de lever — Claude s'adaptera.

    Args:
        range_period: période historique (ex "5d", "1mo")
        interval: intervalle des points (ex "1h", "30m")
    """
    owns_client = client is None
    http_client = client or httpx.Client(
        timeout=DEFAULT_TIMEOUT,
        headers={"User-Agent": "Mozilla/5.0 (trading-engine)"},
    )
    try:
        params = {"range": range_period, "interval": interval, "includePrePost": "false"}
        response = http_client.get(YAHOO_CHART_URL, params=params)
        if response.status_code != 200:
            logger.warning("Yahoo Finance DXY status %s", response.status_code)
            return _dxy_unavailable(f"HTTP {response.status_code}")

        data = response.json()
        return _parse_dxy_response(data)
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        logger.warning("DXY récupération échouée: %s", exc)
        return _dxy_unavailable(str(exc))
    finally:
        if owns_client:
            http_client.close()


def _parse_dxy_response(data: Dict) -> Dict:
    """Parse la réponse Yahoo Finance format v8/chart."""
    result = data.get("chart", {}).get("result")
    if not result:
        return _dxy_unavailable("chart.result vide")

    r = result[0]
    meta = r.get("meta", {})
    timestamps = r.get("timestamp", [])
    quote = r.get("indicators", {}).get("quote", [{}])[0]
    closes_raw = quote.get("close", [])

    # Filtrer les valeurs None (bougies incomplètes)
    valid_pairs = [(t, c) for t, c in zip(timestamps, closes_raw) if c is not None]
    if not valid_pairs:
        return _dxy_unavailable("aucune clôture valide")

    closes = [c for _, c in valid_pairs]
    current_price = float(meta.get("regularMarketPrice", closes[-1]))
    previous_close = float(meta.get("chartPreviousClose", closes[0]))

    # Variation 24h : dernière valeur vs valeur 24h avant si dispo, sinon vs previous_close
    price_24h_ago = closes[-24] if len(closes) >= 24 else closes[0]
    change_24h_pct = ((current_price - price_24h_ago) / price_24h_ago) * 100

    price_7d_ago = closes[0]
    change_7d_pct = ((current_price - price_7d_ago) / price_7d_ago) * 100

    direction = _direction_from_change(change_24h_pct)

    return {
        "available": True,
        "current_price": round(current_price, 3),
        "previous_close": round(previous_close, 3),
        "change_24h_pct": round(change_24h_pct, 3),
        "change_7d_pct": round(change_7d_pct, 3),
        "direction_24h": direction,
        "last_24h_hourly_closes": [round(c, 3) for c in closes[-24:]],
        "note_for_claude": (
            "L'or est historiquement inversement corrélé au DXY "
            "(corrélation -0.7 à -0.9 en moyenne). Un DXY qui monte fortement "
            "est un vent contraire pour les trades XAUUSD haussiers."
        ),
    }


def _direction_from_change(pct: float) -> str:
    if pct > 0.3:
        return "haussiere"
    if pct < -0.3:
        return "baissiere"
    return "neutre"


def _dxy_unavailable(reason: str) -> Dict:
    return {
        "available": False,
        "reason": reason,
        "note_for_claude": "Contexte DXY indisponible ce cycle — analyse sans cette information.",
    }
