"""
supertrend.py

SuperTrend (Olivier Seban) — reproduction fidèle de ta.supertrend() de Pine v5.
Voir supertrend.pine pour la source Pine.

Retourne deux séries: (supertrend_line, direction)
- supertrend_line : niveau de la ligne
- direction       : +1 (haussier, ligne verte sous prix) ou -1 (baissier, ligne rouge au-dessus)

Convention Pine: direction = -1 quand haussier, +1 quand baissier.
Ici on renvoie l'inverse pour lisibilité: +1 haussier, -1 baissier.

Note look-ahead: SuperTrend[t] utilise close[t-1] et supertrend[t-1], donc
strictement causal. Aucune fuite.
"""

from __future__ import annotations
import numpy as np

from .atr import atr as compute_atr


def supertrend(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    length: int = 10,
    factor: float = 3.0,
) -> tuple[np.ndarray, np.ndarray]:
    """Retourne (supertrend_line, direction) avec direction ∈ {+1, -1}.

    direction[t] = +1 → haussier (SuperTrend est un support sous prix)
    direction[t] = -1 → baissier (SuperTrend est une résistance au-dessus)
    NaN pour i < length-1 (warmup ATR).
    """
    n = len(close)
    a = compute_atr(high, low, close, length)
    hl2 = (high + low) / 2.0
    upper = hl2 - factor * a  # borne inférieure (support en tendance haussière)
    lower = hl2 + factor * a  # borne supérieure (résistance en tendance baissière)

    st = np.full(n, np.nan, dtype=np.float64)
    direction = np.zeros(n, dtype=np.int8)

    # Trouver le premier index valide (ATR non-NaN)
    start = length - 1
    if start >= n:
        return st, direction

    # Init : on ne peut pas décider avant, on prend haussier par défaut
    st[start] = upper[start]
    direction[start] = 1

    for i in range(start + 1, n):
        prev_st = st[i - 1]
        prev_dir = direction[i - 1]
        prev_close = close[i - 1]

        # Ajustement stair-step des bornes
        if prev_dir == 1:  # tendance haussière : on suit le upper (support)
            # up est monotone croissant tant que le trend est haussier
            up = max(upper[i], prev_st) if prev_close > prev_st else upper[i]
            # Test de flip : cassure baissière si close passe SOUS le support
            if close[i] < up:
                direction[i] = -1
                st[i] = lower[i]
            else:
                direction[i] = 1
                st[i] = up
        else:  # tendance baissière : on suit le lower (résistance)
            dn = min(lower[i], prev_st) if prev_close < prev_st else lower[i]
            if close[i] > dn:
                direction[i] = 1
                st[i] = upper[i]
            else:
                direction[i] = -1
                st[i] = dn

    return st, direction
