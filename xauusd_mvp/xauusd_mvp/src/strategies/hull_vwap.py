"""
hull_vwap.py — Candidate 2

Logique mean-reversion vers VWAP:
  ENTRY BUY :
    - Prix M5 casse la VWAP par le bas (close < vwap et prev_close >= vwap)
    - Hull MA(M5) haussière (hull[t] > hull[t-1])
    - Session autorisée
  ENTRY SELL : symétrique (casse VWAP par le haut, Hull baissier)

  SL = entry - sl_k * ATR(M5)   (BUY)   ou entry + sl_k * ATR(M5) (SELL)
       → SL basé sur volatilité, pas sur 5 bougies (trop serré sur XAUUSD)
  TP = VWAP au moment de l'entrée

Note dimensionnement 2026 (XAUUSD ~4000 $, ATR M5 ~5$):
  - SL par défaut à sl_k=1.5 × ATR (~7.5 $) = plus large que le bruit intra-M5
  - TP = VWAP → R:R variable, on garde le contrôle avec MIN_RISK_REWARD dans le moteur
  - min_distance_to_vwap = 5 $ pour ne pas entrer trop près de la cible
"""

from __future__ import annotations
from dataclasses import dataclass
import numpy as np
import polars as pl

from .base import BaseStrategy, StrategyConfig, session_mask_from_ts
from ..indicators.hull_ma import hull_ma
from ..indicators.vwap import session_vwap
from ..indicators.atr import atr


@dataclass
class HullVwapConfig(StrategyConfig):
    hull_length: int = 21
    atr_length: int = 14
    sl_k: float = 1.5                # SL = entry ± sl_k * ATR_M5
    min_distance_to_vwap: float = 5.0  # USD (or 2026)


class HullVwapStrategy(BaseStrategy):
    def __init__(self, cfg: HullVwapConfig | None = None):
        super().__init__(cfg or HullVwapConfig(name="hull_vwap"))

    def compute_signals(self, df_m5: pl.DataFrame, df_h1: pl.DataFrame) -> pl.DataFrame:
        cfg: HullVwapConfig = self.cfg  # type: ignore

        h = df_m5["high"].to_numpy()
        l = df_m5["low"].to_numpy()
        c = df_m5["close"].to_numpy()
        v = df_m5["tick_volume"].to_numpy().astype(float)
        ts = df_m5["ts"].to_numpy()

        hull = hull_ma(c, cfg.hull_length)
        vwap = session_vwap(ts, h, l, c, v)
        a_m5 = atr(h, l, c, cfg.atr_length)

        n = len(c)
        signal = np.array(["HOLD"] * n, dtype="<U4")
        sl = np.full(n, np.nan)
        tp = np.full(n, np.nan)

        session_mask = session_mask_from_ts(df_m5["ts"]).to_numpy()

        for i in range(1, n):
            if np.isnan(hull[i]) or np.isnan(hull[i - 1]) or np.isnan(vwap[i]) or np.isnan(vwap[i - 1]):
                continue
            if np.isnan(a_m5[i]):
                continue
            if not session_mask[i]:
                continue

            distance = abs(c[i] - vwap[i])
            if distance < cfg.min_distance_to_vwap:
                continue

            hull_rising = hull[i] > hull[i - 1]
            crossed_down = c[i - 1] >= vwap[i - 1] and c[i] < vwap[i]
            crossed_up = c[i - 1] <= vwap[i - 1] and c[i] > vwap[i]

            if crossed_down and hull_rising:
                signal[i] = "BUY"
                sl[i] = c[i] - cfg.sl_k * a_m5[i]
                tp[i] = vwap[i]
            elif crossed_up and not hull_rising:
                signal[i] = "SELL"
                sl[i] = c[i] + cfg.sl_k * a_m5[i]
                tp[i] = vwap[i]

        return df_m5.with_columns([
            pl.Series("signal", signal),
            pl.Series("sl", sl),
            pl.Series("tp", tp),
        ])
