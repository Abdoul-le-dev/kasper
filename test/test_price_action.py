import sys
import os
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from trading_engine import zones as zn


def make_candle(o, h, l, c):
    return {"open": o, "high": h, "low": l, "close": c}


def test_find_pivots_detects_clear_high_and_low():
    # Séquence: montée, pic à l'index 5, redescente, creux à l'index 10, remontée
    prices = [100, 101, 102, 103, 104, 110, 104, 103, 102, 101, 90, 95, 98, 100, 102]
    candles = [make_candle(p, p + 1, p - 1, p) for p in prices]
    pivots = zn.find_pivots(candles, left=3, right=3)
    assert 110 in [round(h, 2) for h in [c + 1 for c in [110]]] or True  # sanity
    assert any(h >= 110 for h in pivots["highs"])
    assert any(l <= 90 for l in pivots["lows"])


def test_find_pivots_empty_on_flat_series():
    candles = [make_candle(100, 101, 99, 100) for _ in range(10)]
    pivots = zn.find_pivots(candles, left=3, right=3)
    # Sur un marché parfaitement plat, tout est à la fois max et min local -> pivots partout
    # On vérifie juste qu'il n'y a pas d'erreur et que la structure est cohérente
    assert isinstance(pivots["highs"], list)
    assert isinstance(pivots["lows"], list)


def test_cluster_levels_groups_close_prices():
    levels = [2000.0, 2000.5, 2001.0, 2050.0, 2050.3]
    clusters = zn.cluster_levels(levels, tolerance_pct=0.001)
    assert len(clusters) == 2
    touches = sorted([c["touches"] for c in clusters], reverse=True)
    assert touches == [3, 2]


def test_cluster_levels_empty_input():
    assert zn.cluster_levels([]) == []


def test_cluster_levels_single_value():
    clusters = zn.cluster_levels([2000.0])
    assert len(clusters) == 1
    assert clusters[0]["touches"] == 1


def test_identify_zones_returns_max_n_zones():
    prices_d1 = [100 + (i % 10) for i in range(60)]
    candles_d1 = [make_candle(p, p + 2, p - 2, p) for p in prices_d1]
    prices_h4 = [100 + (i % 8) for i in range(60)]
    candles_h4 = [make_candle(p, p + 1, p - 1, p) for p in prices_h4]

    zones = zn.identify_zones(candles_d1, candles_h4, max_zones=4)
    assert len(zones) <= 4
    for z in zones:
        assert z["type"] in ("resistance", "support")


def test_nearest_zone_picks_closest():
    zones = [
        {"price": 2000.0, "type": "support", "touches": 2},
        {"price": 2050.0, "type": "resistance", "touches": 3},
    ]
    nearest = zn.nearest_zone(2010.0, zones)
    assert nearest["price"] == 2000.0


def test_nearest_zone_empty_returns_none():
    assert zn.nearest_zone(2000.0, []) is None


def test_zone_proximity_type_within_range():
    zones = [{"price": 2000.0, "type": "support", "touches": 2}]
    result = zn.zone_proximity_type(price=2000.5, zones=zones, atr_value=2.0, max_distance_factor=0.3)
    assert result == "demand"


def test_zone_proximity_type_out_of_range_returns_none():
    zones = [{"price": 2000.0, "type": "support", "touches": 2}]
    result = zn.zone_proximity_type(price=2010.0, zones=zones, atr_value=2.0, max_distance_factor=0.3)
    assert result is None


def test_zone_proximity_type_resistance_gives_supply():
    zones = [{"price": 2000.0, "type": "resistance", "touches": 2}]
    result = zn.zone_proximity_type(price=1999.8, zones=zones, atr_value=2.0, max_distance_factor=0.3)
    assert result == "supply"