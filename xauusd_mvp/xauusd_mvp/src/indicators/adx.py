"""
adx.py

ADX/DMI de Wilder — reproduction fidèle de ta.dmi() de Pine v5.
Voir adx.pine pour la source.

Retourne (plus_di, minus_di, adx). Tous en pourcentage (0-100).

Note look-ahead: chaque valeur à t utilise uniquement les données ≤ t. Aucune fuite.
"""

from __future__ import annotations
import numpy as np

from .atr import true_range, rma


def dmi(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    di_length: int = 14,
    adx_length: int = 14,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Retourne (+DI, -DI, ADX)."""
    n = len(close)
    plus_dm = np.zeros(n, dtype=np.float64)
    minus_dm = np.zeros(n, dtype=np.float64)

    if n > 1:
        up_move = high[1:] - high[:-1]
        down_move = low[:-1] - low[1:]
        plus_dm[1:] = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
        minus_dm[1:] = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    tr = true_range(high, low, close)
    tr_smooth = rma(tr, di_length)
    plus_smooth = rma(plus_dm, di_length)
    minus_smooth = rma(minus_dm, di_length)

    with np.errstate(divide='ignore', invalid='ignore'):
        plus_di = 100.0 * plus_smooth / tr_smooth
        minus_di = 100.0 * minus_smooth / tr_smooth
        di_sum = plus_di + minus_di
        dx = np.where(di_sum > 0, 100.0 * np.abs(plus_di - minus_di) / di_sum, 0.0)

    adx = rma(dx, adx_length)
    return plus_di, minus_di, adx
