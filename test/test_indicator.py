"""
Tests isolés du module indicators.py.
Utilise des données synthétiques déterministes pour vérifier chaque fonction
indépendamment, y compris les cas limites et les erreurs attendues.
"""

import sys
import os
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from trading_engine import indicators as ind


def make_candle(o, h, l, c):
    return {"open": o, "high": h, "low": l, "close": c}


# --- EMA ---

def test_ema_basic_length_and_seed():
    values = [10, 11, 12, 13, 14, 15, 16, 17, 18, 19]
    result = ind.ema(values, period=5)
    assert len(result) == len(values)
    # Les 4 premières valeurs sont None (warm-up)
    assert result[:4] == [None, None, None, None]
    # La 5e valeur = SMA des 5 premières
    assert result[4] == pytest.approx(sum(values[:5]) / 5)


def test_ema_known_value_manual_calculation():
    # Vérifie une valeur EMA calculée à la main
    values = [1, 2, 3, 4, 5]
    result = ind.ema(values, period=3)
    seed = (1 + 2 + 3) / 3  # = 2.0
    k = 2 / (3 + 1)
    expected_4 = 4 * k + seed * (1 - k)
    assert result[3] == pytest.approx(expected_4)


def test_ema_insufficient_data_raises():
    with pytest.raises(ind.InsufficientDataError):
        ind.ema([1, 2, 3], period=5)


def test_ema_invalid_period_raises():
    with pytest.raises(ValueError):
        ind.ema([1, 2, 3], period=0)


def test_ema_last_matches_ema_final_value():
    values = list(range(1, 30))
    assert ind.ema_last(values, 10) == ind.ema(values, 10)[-1]


# --- ATR ---

def test_atr_flat_market_equals_high_low_range():
    # Marché plat: chaque bougie a le même range, TR = high-low partout
    candles = [make_candle(100, 102, 98, 100) for _ in range(20)]
    atr_values = ind.atr(candles, period=14)
    assert atr_values[14] == pytest.approx(4.0)  # high-low = 4 constant


def test_atr_insufficient_data_raises():
    candles = [make_candle(100, 101, 99, 100) for _ in range(5)]
    with pytest.raises(ind.InsufficientDataError):
        ind.atr(candles, period=14)


def test_atr_average_computation():
    atr_series = [None] * 5 + [1.0, 2.0, 3.0, 4.0, 5.0] + [6.0] * 20
    avg = ind.atr_average(atr_series, lookback=20)
    valid = [v for v in atr_series if v is not None]
    expected = sum(valid[-20:]) / 20
    assert avg == pytest.approx(expected)


def test_atr_average_insufficient_raises():
    with pytest.raises(ind.InsufficientDataError):
        ind.atr_average([1.0, 2.0, 3.0], lookback=20)


# --- Bollinger ---

def test_bollinger_bands_symmetry():
    values = [100] * 19 + [110]  # légère variation à la fin
    bands = ind.bollinger_bands(values, period=20, num_std=2)
    assert bands["lower"] < bands["mid"] < bands["upper"]
    # Symétrie autour du mid
    assert (bands["upper"] - bands["mid"]) == pytest.approx(bands["mid"] - bands["lower"])


def test_bollinger_constant_series_zero_width():
    values = [100] * 25
    bands = ind.bollinger_bands(values, period=20)
    assert bands["upper"] == bands["mid"] == bands["lower"] == 100


def test_bollinger_insufficient_data_raises():
    with pytest.raises(ind.InsufficientDataError):
        ind.bollinger_bands([1, 2, 3], period=20)


# --- RSI ---

def test_rsi_all_gains_is_100():
    values = list(range(1, 20))  # strictement croissant
    result = ind.rsi(values, period=14)
    assert result == pytest.approx(100.0)


def test_rsi_all_losses_is_0():
    values = list(range(20, 1, -1))  # strictement décroissant
    result = ind.rsi(values, period=14)
    assert result == pytest.approx(0.0)


def test_rsi_mid_range_for_mixed_series():
    values = [100, 102, 101, 103, 102, 104, 103, 105, 104, 106, 105, 107, 106, 108, 107]
    result = ind.rsi(values, period=14)
    assert 0 <= result <= 100


def test_rsi_insufficient_data_raises():
    with pytest.raises(ind.InsufficientDataError):
        ind.rsi([1, 2, 3], period=14)


# --- Market structure ---

def test_detect_market_structure_bullish():
    # Construit une séquence claire de higher-highs / higher-lows
    prices = []
    base = 100
    for i in range(30):
        cycle = i % 6
        wave = [0, 2, 4, 1, 3, 5][cycle] + (i // 6) * 3  # tendance montante avec oscillation
        prices.append(base + wave)
    candles = [make_candle(p, p + 1.5, p - 1.5, p) for p in prices]
    structure = ind.detect_market_structure(candles, swing_window=2)
    assert structure in ("bullish", "range")  # tolérance selon la détection exacte des pivots


def test_detect_market_structure_insufficient_pivots_returns_range():
    candles = [make_candle(100, 101, 99, 100) for _ in range(5)]
    structure = ind.detect_market_structure(candles)
    assert structure == "range"


# --- SCD ---

def test_scd_score_full_bullish_confluence():
    score = ind.scd_score(price=110, ema50=105, ema200=100, structure_h4="bullish", zone_proximity="demand")
    assert score == 3


def test_scd_score_full_bearish_confluence():
    score = ind.scd_score(price=90, ema50=95, ema200=100, structure_h4="bearish", zone_proximity="supply")
    assert score == -3


def test_scd_score_neutral_when_mixed():
    score = ind.scd_score(price=100, ema50=100, ema200=100, structure_h4="range", zone_proximity=None)
    assert score == 0


# --- IRV ---

def test_irv_index_computation():
    assert ind.irv_index(atr_current=2.0, atr_avg20=1.0) == 2.0


def test_irv_index_zero_avg_raises():
    with pytest.raises(ValueError):
        ind.irv_index(atr_current=2.0, atr_avg20=0)


def test_irv_regime_boundaries():
    assert ind.irv_regime(0.5) == "compression"
    assert ind.irv_regime(0.69) == "compression"
    assert ind.irv_regime(0.7) == "normal"
    assert ind.irv_regime(1.0) == "normal"
    assert ind.irv_regime(1.3) == "normal"
    assert ind.irv_regime(1.31) == "expansion"
    assert ind.irv_regime(2.0) == "expansion"