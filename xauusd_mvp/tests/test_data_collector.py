"""Tests unitaires du data_collector — sans I/O réseau."""

from datetime import datetime, timezone, date
import struct
import lzma

import pytest

from src.data_collector import (
    dukascopy_url,
    iter_hours,
    parse_bi5,
    ticks_to_m1,
    TICK_STRUCT,
)


def test_url_month_zero_indexed():
    # Janvier = mois 00 chez Dukascopy
    dt = datetime(2024, 1, 15, 13, tzinfo=timezone.utc)
    url = dukascopy_url(dt, "XAUUSD")
    assert "/2024/00/15/13h_ticks.bi5" in url

    # Décembre = mois 11
    dt = datetime(2024, 12, 5, 8, tzinfo=timezone.utc)
    url = dukascopy_url(dt, "XAUUSD")
    assert "/2024/11/05/08h_ticks.bi5" in url


def test_iter_hours_skips_weekend():
    # Samedi 6 janvier 2024
    start = date(2024, 1, 6)
    end = date(2024, 1, 6)
    hours = list(iter_hours(start, end))
    assert hours == []

    # Vendredi 5 janvier 2024 : garde jusqu'à 21h UTC inclus
    hours = list(iter_hours(date(2024, 1, 5), date(2024, 1, 5)))
    max_hour = max(h.hour for h in hours)
    assert max_hour == 21  # 22h et au-delà = fermé


def test_iter_hours_full_business_day_has_24_hours():
    # Mardi 9 janvier 2024
    hours = list(iter_hours(date(2024, 1, 9), date(2024, 1, 9)))
    assert len(hours) == 24


def test_parse_bi5_empty():
    assert parse_bi5(b"", datetime(2024, 1, 9, 13, tzinfo=timezone.utc)) == []


def test_parse_bi5_valid():
    # Simule 2 ticks
    hour = datetime(2024, 6, 3, 10, tzinfo=timezone.utc)
    tick1 = TICK_STRUCT.pack(500, 2050000, 2049500, 1.5, 1.2)      # +500ms
    tick2 = TICK_STRUCT.pack(60000, 2051000, 2050500, 1.0, 1.0)    # +60s
    raw = lzma.compress(tick1 + tick2)

    ticks = parse_bi5(raw, hour)
    assert len(ticks) == 2
    ts, bid, ask, bv, av = ticks[0]
    assert bid == 2049.5
    assert ask == 2050.0
    assert ts.tzinfo is not None


def test_ticks_to_m1_ohlc_correct():
    hour = datetime(2024, 6, 3, 10, tzinfo=timezone.utc)
    ticks = [
        (hour, 2050.0, 2050.4, 1, 1),               # mid = 2050.2
        (hour.replace(second=30), 2051.0, 2051.4, 1, 1),  # mid = 2051.2 (high)
        (hour.replace(second=45), 2049.0, 2049.4, 1, 1),  # mid = 2049.2 (low)
    ]
    bars = ticks_to_m1(ticks)
    assert len(bars) == 1
    b = bars[0]
    assert b.open == pytest.approx(2050.2)
    assert b.high == pytest.approx(2051.2)
    assert b.low == pytest.approx(2049.2)
    assert b.close == pytest.approx(2049.2)
    assert b.tick_volume == 3
