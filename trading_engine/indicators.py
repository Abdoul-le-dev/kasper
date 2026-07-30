"""
indicators.py

Indicateurs techniques du système de trading XAUUSD.
Toutes les fonctions sont pures (pas d'effets de bord) pour faciliter les tests isolés.

Conventions:
- Une bougie OHLC est un dict: {"open": float, "high": float, "low": float, "close": float}
- Les listes de bougies sont ordonnées du plus ancien au plus récent (index -1 = dernière bougie).
"""

from typing import List, Dict, Optional


class InsufficientDataError(Exception):
    """Levée quand il n'y a pas assez de données pour calculer un indicateur."""
    pass


def ema(values: List[float], period: int) -> List[float]:
    """
    Calcule l'EMA (Exponential Moving Average) sur une série de prix.

    Retourne une liste de même longueur que `values`, où les `period-1`
    premières valeurs sont calculées avec une SMA de warm-up progressive.
    """
    if period <= 0:
        raise ValueError("period doit être > 0")
    if len(values) < period:
        raise InsufficientDataError(
            f"EMA({period}) nécessite au moins {period} valeurs, {len(values)} fournies"
        )

    k = 2 / (period + 1)
    result = []
    # Amorçage avec une SMA sur les `period` premières valeurs
    sma_seed = sum(values[:period]) / period
    result.extend([None] * (period - 1))
    result.append(sma_seed)

    prev = sma_seed
    for price in values[period:]:
        curr = price * k + prev * (1 - k)
        result.append(curr)
        prev = curr

    return result


def ema_last(values: List[float], period: int) -> float:
    """Retourne uniquement la dernière valeur de l'EMA."""
    values_ema = ema(values, period)
    return values_ema[-1]


def true_range(candle: Dict, prev_close: Optional[float]) -> float:
    """Calcule le True Range d'une bougie par rapport à la clôture précédente."""
    high, low = candle["high"], candle["low"]
    if prev_close is None:
        return high - low
    return max(
        high - low,
        abs(high - prev_close),
        abs(low - prev_close),
    )


def atr(ohlc: List[Dict], period: int = 14) -> List[float]:
    """
    Calcule l'ATR (Average True Range) sur une série de bougies OHLC.
    Retourne une liste alignée avec `ohlc` (None tant que la fenêtre n'est pas remplie).
    """
    if len(ohlc) < period + 1:
        raise InsufficientDataError(
            f"ATR({period}) nécessite au moins {period + 1} bougies, {len(ohlc)} fournies"
        )

    trs = []
    for i, candle in enumerate(ohlc):
        prev_close = ohlc[i - 1]["close"] if i > 0 else None
        trs.append(true_range(candle, prev_close))

    result = [None] * period
    first_atr = sum(trs[1:period + 1]) / period  # on ignore trs[0] (pas de close précédente fiable)
    result.append(first_atr)

    prev_atr = first_atr
    for tr in trs[period + 1:]:
        curr_atr = (prev_atr * (period - 1) + tr) / period
        result.append(curr_atr)
        prev_atr = curr_atr

    return result


def atr_last(ohlc: List[Dict], period: int = 14) -> float:
    """Retourne la dernière valeur d'ATR."""
    return atr(ohlc, period)[-1]


def atr_average(atr_values: List[float], lookback: int = 20) -> float:
    """
    Calcule la moyenne de l'ATR sur les `lookback` dernières valeurs valides
    (utilisé pour l'IRV — Indice de Régime de Volatilité).
    """
    valid = [v for v in atr_values if v is not None]
    if len(valid) < lookback:
        raise InsufficientDataError(
            f"atr_average nécessite au moins {lookback} valeurs d'ATR valides, {len(valid)} fournies"
        )
    window = valid[-lookback:]
    return sum(window) / lookback


def bollinger_bands(values: List[float], period: int = 20, num_std: float = 2.0) -> Dict[str, float]:
    """
    Calcule les bandes de Bollinger sur la dernière fenêtre de `period` valeurs.
    Retourne {"upper": ..., "mid": ..., "lower": ...}.
    """
    if len(values) < period:
        raise InsufficientDataError(
            f"Bollinger({period}) nécessite au moins {period} valeurs, {len(values)} fournies"
        )
    window = values[-period:]
    mid = sum(window) / period
    variance = sum((v - mid) ** 2 for v in window) / period
    std = variance ** 0.5
    return {
        "upper": mid + num_std * std,
        "mid": mid,
        "lower": mid - num_std * std,
    }


def rsi(values: List[float], period: int = 14) -> float:
    """
    Calcule le RSI (Relative Strength Index, méthode de Wilder) sur la dernière valeur disponible.
    """
    if len(values) < period + 1:
        raise InsufficientDataError(
            f"RSI({period}) nécessite au moins {period + 1} valeurs, {len(values)} fournies"
        )

    deltas = [values[i] - values[i - 1] for i in range(1, len(values))]
    gains = [max(d, 0) for d in deltas]
    losses = [max(-d, 0) for d in deltas]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def detect_market_structure(ohlc: List[Dict], swing_window: int = 3) -> str:
    """
    Détecte la structure de marché (haussière / baissière / range) à partir
    d'une séquence de swing highs / swing lows.

    Méthode: identifie les pivots locaux (high/low entourés de `swing_window`
    bougies plus basses/hautes de chaque côté), puis compare les deux derniers
    pivots hauts et les deux derniers pivots bas.

    Retourne: "bullish" | "bearish" | "range"
    """
    highs = [c["high"] for c in ohlc]
    lows = [c["low"] for c in ohlc]
    n = len(ohlc)

    pivot_highs = []
    pivot_lows = []

    for i in range(swing_window, n - swing_window):
        window_highs = highs[i - swing_window:i + swing_window + 1]
        window_lows = lows[i - swing_window:i + swing_window + 1]
        if highs[i] == max(window_highs):
            pivot_highs.append((i, highs[i]))
        if lows[i] == min(window_lows):
            pivot_lows.append((i, lows[i]))

    if len(pivot_highs) < 2 or len(pivot_lows) < 2:
        return "range"

    last_two_highs = pivot_highs[-2:]
    last_two_lows = pivot_lows[-2:]

    higher_high = last_two_highs[1][1] > last_two_highs[0][1]
    higher_low = last_two_lows[1][1] > last_two_lows[0][1]
    lower_high = last_two_highs[1][1] < last_two_highs[0][1]
    lower_low = last_two_lows[1][1] < last_two_lows[0][1]

    if higher_high and higher_low:
        return "bullish"
    if lower_high and lower_low:
        return "bearish"
    return "range"


def scd_score(
    price: float,
    ema50: float,
    ema200: float,
    structure_h4: str,
    zone_proximity: Optional[str],
) -> int:
    """
    Score de Confluence Directionnelle (SCD), de -3 à +3.

    - +1 / -1 : prix > EMA50 > EMA200 (haussier) ou l'inverse (baissier)
    - +1 / -1 : structure H4 bullish / bearish (0 si range)
    - +1 / -1 : proximité d'une zone de demande ("demand") ou d'offre ("supply")

    zone_proximity: "demand" | "supply" | None
    """
    score = 0

    if price > ema50 > ema200:
        score += 1
    elif price < ema50 < ema200:
        score -= 1

    if structure_h4 == "bullish":
        score += 1
    elif structure_h4 == "bearish":
        score -= 1

    if zone_proximity == "demand":
        score += 1
    elif zone_proximity == "supply":
        score -= 1

    return score


def irv_index(atr_current: float, atr_avg20: float) -> float:
    """Indice de Régime de Volatilité = ATR courant / moyenne ATR sur 20 périodes."""
    if atr_avg20 == 0:
        raise ValueError("atr_avg20 ne peut pas être 0")
    return atr_current / atr_avg20


def irv_regime(irv: float) -> str:
    """Classe l'IRV en régime: 'compression' | 'normal' | 'expansion'."""
    if irv < 0.7:
        return "compression"
    if irv > 1.3:
        return "expansion"
    return "normal"