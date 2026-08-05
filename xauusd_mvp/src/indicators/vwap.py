"""
vwap.py

VWAP session-based, ancrage journalier à 00:00 UTC.
Voir vwap.pine pour la source Pine de référence.

Pour XAUUSD (spot forex), l'ancrage journalier UTC est le standard TradingView
et correspond à la "session cachée" du CME sur GC. Le prix typique est (H+L+C)/3.

Note look-ahead: le VWAP à t utilise uniquement les bougies de la session en cours
jusqu'à t inclus. Aucune fuite.
"""

from __future__ import annotations
import numpy as np
from datetime import datetime


def session_vwap(
    ts: np.ndarray,   # array de datetime64[ns] ou de datetime naïfs (UTC)
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    volume: np.ndarray,
) -> np.ndarray:
    """VWAP re-ancré chaque jour à 00:00 UTC.

    Le premier point d'une nouvelle session est le typical price lui-même.
    """
    n = len(close)
    out = np.full(n, np.nan, dtype=np.float64)
    if n == 0:
        return out

    typ = (high + low + close) / 3.0
    cum_pv = 0.0
    cum_v = 0.0
    # Extraire la date de chaque timestamp
    # Support ts en pd.Timestamp / np.datetime64 / datetime.datetime
    def _day(t):
        if isinstance(t, np.datetime64):
            return t.astype('datetime64[D]')
        return t.date()

    prev_day = _day(ts[0])
    for i in range(n):
        cur_day = _day(ts[i])
        if cur_day != prev_day:
            cum_pv = 0.0
            cum_v = 0.0
            prev_day = cur_day
        cum_pv += typ[i] * volume[i]
        cum_v += volume[i]
        if cum_v > 0:
            out[i] = cum_pv / cum_v
    return out
