"""
ttm_adx.py — Candidate 3

Logique breakout:
  ENTRY BUY :
    - Sortie de squeeze : squeeze_on[t-1] == True et squeeze_on[t] == False
    - ADX(M15) > adx_min (confirmation force du move)
    - Close > open sur la bougie de sortie (direction haussière du move)
    - Session autorisée
  ENTRY SELL : symétrique (close < open)

  SL = opposé de la bougie de sortie du squeeze (low pour BUY, high pour SELL)
  TP = entry ± k * ATR(M5)
"""

from __future__ import annotations
from dataclasses import dataclass
import numpy as np
import polars as pl

from .base import BaseStrategy, StrategyConfig, session_mask_from_ts
from ..indicators.ttm_squeeze import ttm_squeeze
from ..indicators.adx import dmi
from ..indicators.atr import atr


@dataclass
class TtmAdxConfig(StrategyConfig):
    squeeze_length: int = 20
    mult_bb: float = 2.0
    mult_kc: float = 1.5
    adx_min: float = 20.0
    atr_length: int = 14
    # Dimensionnement XAUUSD 2026 (ATR M5 ~5$):
    # SL au low/high de la M5 de breakout est ~5-8$, trop serré → mangé par bruit.
    # On élargit à sl_k × ATR pour laisser respirer.
    # sl_k=2.0 → SL à ~10$, tp_k=3.5 → TP à ~17.5$, R:R = 1.75 : conforme.
    sl_k: float = 2.0
    tp_k: float = 3.5


class TtmAdxStrategy(BaseStrategy):
    def __init__(self, cfg: TtmAdxConfig | None = None):
        super().__init__(cfg or TtmAdxConfig(name="ttm_adx"))

    def compute_signals(self, df_m5: pl.DataFrame, df_h1: pl.DataFrame) -> pl.DataFrame:
        cfg: TtmAdxConfig = self.cfg  # type: ignore

        # Note: pour la simplicité et pour respecter "M1 supprimé", on utilise
        # M5 pour tout (squeeze, ADX). Le "contexte" H1 est disponible mais
        # non utilisé ici — cette stratégie fonctionne intra-timeframe M5.
        o = df_m5["open"].to_numpy()
        h = df_m5["high"].to_numpy()
        l = df_m5["low"].to_numpy()
        c = df_m5["close"].to_numpy()

        squeeze_on, _sq_off = ttm_squeeze(h, l, c, cfg.squeeze_length, cfg.mult_bb, cfg.mult_kc)
        _p, _m, adx = dmi(h, l, c, di_length=14, adx_length=14)
        a_m5 = atr(h, l, c, cfg.atr_length)

        n = len(c)
        signal = np.array(["HOLD"] * n, dtype="<U4")
        sl = np.full(n, np.nan)
        tp = np.full(n, np.nan)

        session_mask = session_mask_from_ts(df_m5["ts"]).to_numpy()

        for i in range(1, n):
            if not session_mask[i]:
                continue
            if np.isnan(adx[i]) or adx[i] < cfg.adx_min:
                continue
            if np.isnan(a_m5[i]):
                continue

            # Sortie de squeeze : était compressé à t-1, ne l'est plus à t
            just_fired = squeeze_on[i - 1] and not squeeze_on[i]
            if not just_fired:
                continue

            if c[i] > o[i]:
                signal[i] = "BUY"
                sl[i] = c[i] - cfg.sl_k * a_m5[i]
                tp[i] = c[i] + cfg.tp_k * a_m5[i]
            elif c[i] < o[i]:
                signal[i] = "SELL"
                sl[i] = c[i] + cfg.sl_k * a_m5[i]
                tp[i] = c[i] - cfg.tp_k * a_m5[i]

        return df_m5.with_columns([
            pl.Series("signal", signal),
            pl.Series("sl", sl),
            pl.Series("tp", tp),
        ])
