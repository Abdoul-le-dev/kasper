"""Tests des indicateurs : correction basique + audit look-ahead.

Le test look-ahead est critique : on décale les données de N pas et on vérifie
que la valeur à t ne change pas. Un indicateur qui utilise le futur échoue.
"""

import numpy as np
import pytest

from src.indicators.atr import atr, true_range, rma
from src.indicators.supertrend import supertrend
from src.indicators.hull_ma import hull_ma, wma
from src.indicators.adx import dmi
from src.indicators.ttm_squeeze import ttm_squeeze
from src.indicators.vwap import session_vwap


# ---------- Données synthétiques ----------

def make_trending_series(n=500, start=2000.0, drift=0.05, noise=0.5, seed=42):
    """OHLC synthétique avec drift haussier."""
    rng = np.random.default_rng(seed)
    close = start + np.cumsum(rng.normal(drift, noise, n))
    high = close + np.abs(rng.normal(0, 0.3, n))
    low = close - np.abs(rng.normal(0, 0.3, n))
    open_ = np.roll(close, 1)
    open_[0] = start
    volume = rng.integers(100, 500, n).astype(np.float64)
    return open_, high, low, close, volume


# ---------- ATR ----------

def test_atr_no_lookahead():
    _o, h, l, c, _v = make_trending_series(300)
    full = atr(h, l, c, length=14)
    # Recalcul en ne connaissant que les 200 premiers
    truncated = atr(h[:200], l[:200], c[:200], length=14)
    np.testing.assert_allclose(full[:200], truncated, equal_nan=True)


def test_atr_warmup_nan():
    _o, h, l, c, _v = make_trending_series(100)
    a = atr(h, l, c, length=14)
    assert np.all(np.isnan(a[:13]))  # length-1 = 13
    assert not np.isnan(a[13])


def test_true_range_positive():
    _o, h, l, c, _v = make_trending_series(50)
    tr = true_range(h, l, c)
    assert np.all(tr >= 0)


def test_rma_matches_pine_formula():
    x = np.array([1.0, 2, 3, 4, 5, 6, 7, 8, 9, 10])
    r = rma(x, 3)
    assert np.isnan(r[0]) and np.isnan(r[1])
    assert r[2] == pytest.approx(2.0)                       # SMA(1,2,3)
    assert r[3] == pytest.approx((2.0 * 2 + 4) / 3)         # (r[2]*2 + x[3])/3
    assert r[4] == pytest.approx((r[3] * 2 + 5) / 3)


# ---------- SuperTrend ----------

def test_supertrend_no_lookahead():
    _o, h, l, c, _v = make_trending_series(400)
    full_st, full_dir = supertrend(h, l, c, 10, 3.0)
    trunc_st, trunc_dir = supertrend(h[:250], l[:250], c[:250], 10, 3.0)
    np.testing.assert_allclose(full_st[:250], trunc_st, equal_nan=True)
    np.testing.assert_array_equal(full_dir[:250], trunc_dir)


def test_supertrend_direction_values():
    _o, h, l, c, _v = make_trending_series(300)
    _st, direction = supertrend(h, l, c, 10, 3.0)
    valid = direction[9:]  # après warmup
    assert set(np.unique(valid)).issubset({-1, 1})


def test_supertrend_captures_trend():
    _o, h, l, c, _v = make_trending_series(500, drift=0.5, noise=0.3, seed=1)
    _st, direction = supertrend(h, l, c, 10, 3.0)
    # Avec drift très haussier, on doit passer la plupart du temps direction=+1
    ratio_up = np.mean(direction[100:] == 1)
    assert ratio_up > 0.7


# ---------- Hull MA ----------

def test_hull_ma_no_lookahead():
    _o, _h, _l, c, _v = make_trending_series(300)
    full = hull_ma(c, 21)
    trunc = hull_ma(c[:200], 21)
    np.testing.assert_allclose(full[:200], trunc, equal_nan=True)


def test_wma_matches_pine_formula():
    x = np.array([1.0, 2, 3, 4, 5])
    # WMA(3) au dernier point = (3*1 + 4*2 + 5*3) / (1+2+3) = 26/6
    result = wma(x, 3)
    assert result[-1] == pytest.approx(26 / 6)
    assert np.isnan(result[0]) and np.isnan(result[1])


# ---------- ADX ----------

def test_dmi_no_lookahead():
    _o, h, l, c, _v = make_trending_series(400)
    p_full, m_full, adx_full = dmi(h, l, c)
    p_tr, m_tr, adx_tr = dmi(h[:250], l[:250], c[:250])
    np.testing.assert_allclose(p_full[:250], p_tr, equal_nan=True)
    np.testing.assert_allclose(m_full[:250], m_tr, equal_nan=True)
    np.testing.assert_allclose(adx_full[:250], adx_tr, equal_nan=True)


def test_adx_bounded_0_100():
    _o, h, l, c, _v = make_trending_series(300)
    _p, _m, adx_vals = dmi(h, l, c)
    valid = adx_vals[~np.isnan(adx_vals)]
    assert np.all(valid >= 0) and np.all(valid <= 100)


# ---------- TTM Squeeze ----------

def test_ttm_squeeze_no_lookahead():
    _o, h, l, c, _v = make_trending_series(300)
    on_full, off_full = ttm_squeeze(h, l, c)
    on_tr, off_tr = ttm_squeeze(h[:200], l[:200], c[:200])
    np.testing.assert_array_equal(on_full[:200], on_tr)
    np.testing.assert_array_equal(off_full[:200], off_tr)


def test_ttm_squeeze_mutually_exclusive():
    _o, h, l, c, _v = make_trending_series(300)
    on, off = ttm_squeeze(h, l, c)
    # BB ne peut pas être simultanément à l'intérieur ET à l'extérieur des KC
    assert not np.any(on & off)


# ---------- VWAP ----------

def test_vwap_no_lookahead():
    _o, h, l, c, v = make_trending_series(300)
    from datetime import datetime, timedelta, timezone
    ts = np.array([datetime(2024, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=5 * i)
                   for i in range(300)])
    full = session_vwap(ts, h, l, c, v)
    trunc = session_vwap(ts[:200], h[:200], l[:200], c[:200], v[:200])
    np.testing.assert_allclose(full[:200], trunc)


def test_vwap_resets_at_new_session():
    from datetime import datetime, timedelta, timezone
    # 3 bougies jour 1, 3 bougies jour 2
    ts = np.array([
        datetime(2024, 1, 1, 23, 55, tzinfo=timezone.utc),
        datetime(2024, 1, 2, 0, 0, tzinfo=timezone.utc),
        datetime(2024, 1, 2, 0, 5, tzinfo=timezone.utc),
    ])
    h = np.array([2000.0, 2010, 2020])
    l = np.array([1990.0, 2000, 2010])
    c = np.array([1995.0, 2005, 2015])
    v = np.array([100.0, 100, 100])
    result = session_vwap(ts, h, l, c, v)
    # 1er point est isolé (jour 1) → vwap = typical = (2000+1990+1995)/3
    assert result[0] == pytest.approx(1995.0)
    # 2e point est le début du jour 2 → reset, vwap = typical = (2010+2000+2005)/3
    assert result[1] == pytest.approx(2005.0)
