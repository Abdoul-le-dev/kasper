"""
hull_ma.py

Hull Moving Average (Alan Hull, 2005) — reproduction fidèle de la formule Pine.
Voir hull_ma.pine pour la source.

HMA(n) = WMA(2 * WMA(src, n/2) - WMA(src, n), sqrt(n))

Note look-ahead: WMA n'utilise que les valeurs jusqu'à t inclus. Aucune fuite.
"""

from __future__ import annotations
import math
import numpy as np


def wma(x: np.ndarray, length: int) -> np.ndarray:
    """Weighted Moving Average avec poids linéaires (1..length).

    wma[i] = NaN pour i < length-1
    wma[i] = sum(x[i-length+1..i] * [1,2,...,length]) / sum([1..length])
    """
    n = len(x)
    out = np.full(n, np.nan, dtype=np.float64)
    if n < length:
        return out
    weights = np.arange(1, length + 1, dtype=np.float64)
    denom = weights.sum()
    for i in range(length - 1, n):
        window = x[i - length + 1 : i + 1]
        out[i] = np.dot(window, weights) / denom
    return out


def hull_ma(close: np.ndarray, length: int = 21) -> np.ndarray:
    """Hull Moving Average sur la série close."""
    n2 = int(round(length / 2))
    sqn = int(round(math.sqrt(length)))
    wma_half = wma(close, n2)
    wma_full = wma(close, length)
    diff = 2.0 * wma_half - wma_full
    # diff contient des NaN au début; wma() les propage naturellement
    return wma(diff, sqn)
