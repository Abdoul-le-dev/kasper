"""
Types de base pour les stratégies et helper session.

Une Strategy = classe avec compute_signals(df) → renvoie un DataFrame
enrichi de colonnes: signal ∈ {'BUY','SELL','HOLD'}, sl, tp (float).
Le moteur de backtest se charge de tout le reste (position, PnL, kill switch).
"""

from __future__ import annotations
from dataclasses import dataclass
from typing import Optional
import numpy as np
import polars as pl

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402


LONDON = (config.LONDON_START_HOUR, config.LONDON_END_HOUR)
NY = (config.NY_START_HOUR, config.NY_END_HOUR)
OVERLAP = (max(LONDON[0], NY[0]), min(LONDON[1], NY[1]))


def in_trading_session(hour_utc: int) -> bool:
    """True si l'heure UTC est dans une des sessions autorisées.

    Londres 07-16, NY 13-22, overlap 13-16 — union = 07-22.
    """
    return LONDON[0] <= hour_utc < NY[1]


def session_mask_from_ts(ts_col: pl.Series) -> pl.Series:
    """Retourne un booléen par bougie: True si dans session autorisée."""
    hours = ts_col.dt.hour()
    return (hours >= LONDON[0]) & (hours < NY[1])


@dataclass
class StrategyConfig:
    """Base config partagée entre stratégies."""
    name: str


class BaseStrategy:
    """Toute stratégie doit implémenter compute_signals(df_m5, ctx_h1)."""
    def __init__(self, cfg: StrategyConfig):
        self.cfg = cfg

    def compute_signals(self, df_m5: pl.DataFrame, df_h1: pl.DataFrame) -> pl.DataFrame:
        raise NotImplementedError
