"""
atr.py

Average True Range (Wilder, 1978) — reproduction fidèle de ta.atr() de Pine Script v5.
Utilise le lissage RMA (Wilder's smoothing) et non EMA classique.

Voir atr.pine pour la source Pine de référence.

Note look-ahead: TR[t] utilise close[t-1] (déjà connu à t), donc ATR[t] est
calculable strictement avec les données jusqu'à t inclus. Aucune fuite.
"""

from __future__ import annotations
import numpy as np


def true_range(high: np.ndarray, low: np.ndarray, close: np.ndarray) -> np.ndarray:
    """Retourne la série True Range (même longueur que les entrées).

    TR[0] = high[0] - low[0]   (par convention, pas de close[-1])
    TR[t] = max(high[t]-low[t], |high[t]-close[t-1]|, |low[t]-close[t-1]|)
    """
    n = len(close)
    tr = np.empty(n, dtype=np.float64)
    tr[0] = high[0] - low[0]
    if n > 1:
        prev_close = close[:-1]
        h = high[1:]
        l = low[1:]
        tr[1:] = np.maximum.reduce([
            h - l,
            np.abs(h - prev_close),
            np.abs(l - prev_close),
        ])
    return tr


def rma(x: np.ndarray, length: int) -> np.ndarray:
    """Wilder's smoothing (RMA de Pine).

    rma[i] = NaN pour i < length-1
    rma[length-1] = SMA(x[0:length])
    rma[i]      = (rma[i-1]*(length-1) + x[i]) / length  for i >= length
    """
    n = len(x)
    out = np.full(n, np.nan, dtype=np.float64)
    if n < length:
        return out
    seed = x[:length].mean()
    out[length - 1] = seed
    alpha_num = length - 1
    for i in range(length, n):
        out[i] = (out[i - 1] * alpha_num + x[i]) / length
    return out


def atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, length: int = 14) -> np.ndarray:
    """ATR = RMA(TR, length). Mêmes conventions de NaN que Pine."""
    tr = true_range(high, low, close)
    return rma(tr, length)
