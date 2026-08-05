"""
supertrend_atr.py — Candidate 1

Logique:
  ENTRY BUY :
    - flip SuperTrend M5 haussier (direction[t] == 1 et direction[t-1] == -1)
    - ET session ∈ {Londres, NY, overlap}
    - ET ATR(H1) > seuil_atr_min  (évite mort-plat)
  ENTRY SELL : symétrique

  SL = niveau SuperTrend au moment du flip
  TP = entry ± k * ATR(M5) au moment du flip
  Une position à la fois. Pas de pyramiding.
"""

from __future__ import annotations
from dataclasses import dataclass
import numpy as np
import polars as pl

from .base import BaseStrategy, StrategyConfig, session_mask_from_ts
from ..indicators.supertrend import supertrend
from ..indicators.atr import atr


@dataclass
class SupertrendAtrConfig(StrategyConfig):
    st_length: int = 10
    st_factor: float = 3.0
    atr_length: int = 14
    tp_k: float = 2.0
    h1_atr_min: float = 1.0  # USD (or ~10 pips)


class SupertrendAtrStrategy(BaseStrategy):
    def __init__(self, cfg: SupertrendAtrConfig | None = None):
        super().__init__(cfg or SupertrendAtrConfig(name="supertrend_atr"))

    def compute_signals(self, df_m5: pl.DataFrame, df_h1: pl.DataFrame) -> pl.DataFrame:
        cfg: SupertrendAtrConfig = self.cfg  # type: ignore

        h = df_m5["high"].to_numpy()
        l = df_m5["low"].to_numpy()
        c = df_m5["close"].to_numpy()

        st_line, direction = supertrend(h, l, c, cfg.st_length, cfg.st_factor)
        a_m5 = atr(h, l, c, cfg.atr_length)

        # ATR H1 aligné sur M5 via forward-fill : à chaque bougie M5, on prend
        # la dernière valeur ATR H1 connue à ce timestamp
        h_h1 = df_h1["high"].to_numpy()
        l_h1 = df_h1["low"].to_numpy()
        c_h1 = df_h1["close"].to_numpy()
        a_h1 = atr(h_h1, l_h1, c_h1, cfg.atr_length)

        h1_df = pl.DataFrame({"ts_h1": df_h1["ts"], "atr_h1": a_h1})
        # forward join asof (M5 → H1) : chaque bougie M5 hérite du dernier ATR H1 clôturé
        merged = df_m5.join_asof(
            h1_df, left_on="ts", right_on="ts_h1", strategy="backward"
        )
        atr_h1_aligned = merged["atr_h1"].to_numpy()

        n = len(c)
        signal = np.array(["HOLD"] * n, dtype="<U4")
        sl = np.full(n, np.nan)
        tp = np.full(n, np.nan)

        session_mask = session_mask_from_ts(df_m5["ts"]).to_numpy()

        for i in range(1, n):
            if direction[i - 1] == 0 or direction[i] == 0:
                continue  # warmup
            if not session_mask[i]:
                continue
            if np.isnan(atr_h1_aligned[i]) or atr_h1_aligned[i] < cfg.h1_atr_min:
                continue
            if np.isnan(a_m5[i]):
                continue

            # Flip haussier
            if direction[i] == 1 and direction[i - 1] == -1:
                signal[i] = "BUY"
                sl[i] = st_line[i]
                tp[i] = c[i] + cfg.tp_k * a_m5[i]
            elif direction[i] == -1 and direction[i - 1] == 1:
                signal[i] = "SELL"
                sl[i] = st_line[i]
                tp[i] = c[i] - cfg.tp_k * a_m5[i]

        return df_m5.with_columns([
            pl.Series("signal", signal),
            pl.Series("sl", sl),
            pl.Series("tp", tp),
        ])
