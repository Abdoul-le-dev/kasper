"""
ttm_squeeze.py

TTM Squeeze (John Carter, LazyBear variant) — voir ttm_squeeze.pine.
Détecte les phases de compression (BB à l'intérieur des KC).

Retourne:
  squeeze_on   : bool array, True quand BB est à l'intérieur des KC (compression)
  squeeze_off  : bool array, True quand BB sort des KC (expansion - le "fire")

Note look-ahead: SMA et stdev n'utilisent que les données ≤ t. Aucune fuite.
"""

from __future__ import annotations
import numpy as np

from .atr import true_range


def _sma(x: np.ndarray, length: int) -> np.ndarray:
    n = len(x)
    out = np.full(n, np.nan, dtype=np.float64)
    if n < length:
        return out
    csum = np.cumsum(x, dtype=np.float64)
    out[length - 1] = csum[length - 1] / length
    if n > length:
        out[length:] = (csum[length:] - csum[:-length]) / length
    return out


def _stdev(x: np.ndarray, length: int) -> np.ndarray:
    """Écart-type population (comme Pine ta.stdev par défaut)."""
    n = len(x)
    out = np.full(n, np.nan, dtype=np.float64)
    if n < length:
        return out
    for i in range(length - 1, n):
        window = x[i - length + 1 : i + 1]
        m = window.mean()
        out[i] = np.sqrt(np.mean((window - m) ** 2))
    return out


def ttm_squeeze(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    length: int = 20,
    mult_bb: float = 2.0,
    mult_kc: float = 1.5,
) -> tuple[np.ndarray, np.ndarray]:
    """Retourne (squeeze_on, squeeze_off) sous forme d'arrays booléens."""
    basis = _sma(close, length)
    dev = mult_bb * _stdev(close, length)
    upper_bb = basis + dev
    lower_bb = basis - dev

    tr = true_range(high, low, close)
    range_ma = _sma(tr, length)
    ma = basis  # même SMA
    upper_kc = ma + range_ma * mult_kc
    lower_kc = ma - range_ma * mult_kc

    squeeze_on = (lower_bb > lower_kc) & (upper_bb < upper_kc)
    squeeze_off = (lower_bb < lower_kc) & (upper_bb > upper_kc)
    # NaN warmup zone → False par défaut
    valid = ~np.isnan(upper_bb) & ~np.isnan(upper_kc)
    squeeze_on = squeeze_on & valid
    squeeze_off = squeeze_off & valid
    return squeeze_on, squeeze_off
