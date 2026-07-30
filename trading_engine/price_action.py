"""
price_action.py

Détection de signaux d'action de prix utilisés comme déclencheur d'entrée (M15).
"""

from typing import Dict, List


def candle_body(candle: Dict) -> float:
    return abs(candle["close"] - candle["open"])


def candle_range(candle: Dict) -> float:
    return candle["high"] - candle["low"]


def is_bullish_engulfing(prev: Dict, curr: Dict) -> bool:
    """Bougie haussière dont le corps englobe entièrement le corps de la bougie précédente baissière."""
    prev_bearish = prev["close"] < prev["open"]
    curr_bullish = curr["close"] > curr["open"]
    if not (prev_bearish and curr_bullish):
        return False
    return curr["open"] <= prev["close"] and curr["close"] >= prev["open"]


def is_bearish_engulfing(prev: Dict, curr: Dict) -> bool:
    """Bougie baissière dont le corps englobe entièrement le corps de la bougie précédente haussière."""
    prev_bullish = prev["close"] > prev["open"]
    curr_bearish = curr["close"] < curr["open"]
    if not (prev_bullish and curr_bearish):
        return False
    return curr["open"] >= prev["close"] and curr["close"] <= prev["open"]


def is_bullish_pin_bar(candle: Dict, min_wick_ratio: float = 2.0) -> bool:
    """
    Pin bar haussier (rejet de mèche basse): la mèche basse doit être au moins
    `min_wick_ratio` fois plus longue que le corps, et la clôture proche du haut.
    """
    body = candle_body(candle)
    lower_wick = min(candle["open"], candle["close"]) - candle["low"]
    total_range = candle_range(candle)
    if total_range == 0 or body == 0:
        return False
    if lower_wick < min_wick_ratio * body:
        return False
    # La clôture doit être dans la moitié supérieure de la bougie
    return (candle["close"] - candle["low"]) / total_range > 0.6


def is_bearish_pin_bar(candle: Dict, min_wick_ratio: float = 2.0) -> bool:
    """Pin bar baissier (rejet de mèche haute)."""
    body = candle_body(candle)
    upper_wick = candle["high"] - max(candle["open"], candle["close"])
    total_range = candle_range(candle)
    if total_range == 0 or body == 0:
        return False
    if upper_wick < min_wick_ratio * body:
        return False
    return (candle["high"] - candle["close"]) / total_range > 0.6


def detect_price_action_signal(ohlc_m15: List[Dict], direction: str) -> bool:
    """
    Vérifie si la/les dernière(s) bougie(s) M15 confirment un signal d'entrée
    dans la direction donnée ("BUY" ou "SELL").

    Utilise la dernière bougie clôturée (index -1) et la précédente (index -2)
    pour les patterns d'engulfing, et la dernière bougie seule pour les pin bars.
    """
    if len(ohlc_m15) < 2:
        return False

    prev, curr = ohlc_m15[-2], ohlc_m15[-1]

    if direction == "BUY":
        return is_bullish_engulfing(prev, curr) or is_bullish_pin_bar(curr)
    elif direction == "SELL":
        return is_bearish_engulfing(prev, curr) or is_bearish_pin_bar(curr)
    else:
        raise ValueError("direction doit être 'BUY' ou 'SELL'")