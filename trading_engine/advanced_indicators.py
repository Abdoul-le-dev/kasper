"""
advanced_indicators.py

Indicateurs supplémentaires pour enrichir le dossier envoyé à Claude:
- Niveaux Fibonacci (retracement) du dernier swing significatif
- VWAP (Volume Weighted Average Price) de la session en cours

Fonctions pures, testables isolément.
"""

from typing import Dict, List, Optional


FIB_LEVELS = [0.0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0]
FIB_EXTENSIONS = [1.272, 1.618, 2.0]


def find_last_swing(ohlc: List[Dict], lookback: int = 50) -> Optional[Dict]:
    """
    Identifie le dernier swing significatif (plus haut et plus bas des N dernières
    bougies), utilisé pour tracer les retracements Fibonacci.

    Retourne {"swing_high": float, "swing_low": float, "direction": "up"|"down"}
    Direction: "up" si le low a précédé le high (mouvement montant), sinon "down".
    """
    if len(ohlc) < 5:
        return None

    window = ohlc[-lookback:]
    highs = [c["high"] for c in window]
    lows = [c["low"] for c in window]

    swing_high = max(highs)
    swing_low = min(lows)
    idx_high = highs.index(swing_high)
    idx_low = lows.index(swing_low)

    # Si le low arrive avant le high dans la fenêtre → mouvement montant
    direction = "up" if idx_low < idx_high else "down"

    return {
        "swing_high": round(swing_high, 3),
        "swing_low": round(swing_low, 3),
        "direction": direction,
        "range": round(abs(swing_high - swing_low), 3),
    }


def fibonacci_levels(ohlc: List[Dict], lookback: int = 50) -> Optional[Dict]:
    """
    Calcule les niveaux de retracement Fibonacci du dernier swing.
    Retourne un dict {niveau: prix} pour retracement + extensions.
    """
    swing = find_last_swing(ohlc, lookback=lookback)
    if swing is None:
        return None

    high = swing["swing_high"]
    low = swing["swing_low"]
    range_ = high - low
    direction = swing["direction"]

    if range_ == 0:
        return None

    retracements = {}
    for level in FIB_LEVELS:
        if direction == "up":
            # Retracement depuis le high vers le low : le prix baisse depuis le high
            price = high - level * range_
        else:
            # Retracement depuis le low vers le high : le prix monte depuis le low
            price = low + level * range_
        retracements[f"fib_{level}"] = round(price, 3)

    extensions = {}
    for ext in FIB_EXTENSIONS:
        if direction == "up":
            price = low + ext * range_
        else:
            price = high - ext * range_
        extensions[f"ext_{ext}"] = round(price, 3)

    return {
        "swing_direction": direction,
        "swing_high": high,
        "swing_low": low,
        "retracements": retracements,
        "extensions": extensions,
    }


def vwap(ohlc: List[Dict], volumes: Optional[List[float]] = None) -> Optional[float]:
    """
    Calcule le VWAP (Volume Weighted Average Price) sur la série de bougies fournie.
    Typiquement calé sur la session courante (ex: dernières 24 bougies M15 pour session US).

    Si les volumes ne sont pas fournis, utilise 1 comme poids (équivalent à moyenne des typical prices).
    Typical price = (high + low + close) / 3.
    """
    if not ohlc:
        return None

    n = len(ohlc)
    if volumes is None or len(volumes) != n:
        volumes = [1.0] * n

    total_pv = 0.0
    total_volume = 0.0
    for candle, volume in zip(ohlc, volumes):
        typical = (candle["high"] + candle["low"] + candle["close"]) / 3
        total_pv += typical * volume
        total_volume += volume

    if total_volume == 0:
        return None
    return round(total_pv / total_volume, 3)


def vwap_session(ohlc_m15: List[Dict], session_length_candles: int = 32) -> Optional[Dict]:
    """
    VWAP calé sur les N dernières bougies M15 (par défaut 32 = 8h de session).

    Retourne le VWAP + la distance actuelle en % du prix par rapport au VWAP,
    ce qui permet à Claude de savoir si le prix est au-dessus/en-dessous.
    """
    if len(ohlc_m15) < 2:
        return None

    session_ohlc = ohlc_m15[-session_length_candles:]
    vwap_value = vwap(session_ohlc)
    if vwap_value is None:
        return None

    current_price = session_ohlc[-1]["close"]
    distance_pct = ((current_price - vwap_value) / vwap_value) * 100

    return {
        "vwap": vwap_value,
        "prix_actuel": current_price,
        "distance_pct": round(distance_pct, 3),
        "position": "au-dessus" if current_price > vwap_value else "en-dessous",
        "bougies_utilisees": len(session_ohlc),
    }
