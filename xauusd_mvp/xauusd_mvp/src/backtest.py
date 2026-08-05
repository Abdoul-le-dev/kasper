"""
backtest.py

Moteur de backtest event-driven pour XAUUSD sur M5.

Règles:
- Une seule position à la fois (spec MVP)
- Kill switch : arrêt total des ENTRIES si perte cumulée du jour ≥ 50 $
  (les positions ouvertes sont laissées à leur SL/TP)
- Coûts par trade: commission fixe + spread réel (spread_avg de Dukascopy) + slippage 1 tick
- Sizing: risque fixe = 10 $ par trade (5 trades perdants max ⇒ 50 $ perte max)
- SL / TP simulés bougie par bougie sur les M5 SUIVANTES : si high touche TP → hit TP,
  si low touche SL → hit SL. Si les deux dans la même bougie → on suppose SL touché
  (conservateur, pénalise plutôt que d'embellir).

Usage:
    python -m src.backtest --strategy supertrend_atr [--params key=val ...]
"""

from __future__ import annotations
import argparse
import json
import logging
import sys
from dataclasses import dataclass, asdict, field
from datetime import datetime, date, time, timezone
from pathlib import Path
from typing import Any, Optional

import numpy as np
import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402
from src.strategies.base import BaseStrategy
from src.strategies.supertrend_atr import SupertrendAtrStrategy, SupertrendAtrConfig
from src.strategies.hull_vwap import HullVwapStrategy, HullVwapConfig
from src.strategies.ttm_adx import TtmAdxStrategy, TtmAdxConfig


logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("backtest")


# ---------------------------------------------------------------------------
# Constantes broker
# ---------------------------------------------------------------------------

PIP_SIZE_XAUUSD = 0.1                       # 1 pip = 0.10 $
PIP_VALUE_PER_LOT = 10.0                    # 1 pip pour 1 lot = 10 $
RISK_PER_TRADE_USD = 10.0                   # 5 pertes = 50 $ (kill switch)
MIN_LOT = 0.01
MAX_LOT = 10.0


def compute_lot_size(risk_usd: float, sl_distance_price: float) -> float:
    """Retourne la taille de lot pour risquer risk_usd$ étant donné la
    distance au SL en prix. Bornée à [MIN_LOT, MAX_LOT], arrondie 0.01.
    """
    if sl_distance_price <= 0:
        return 0.0
    sl_pips = sl_distance_price / PIP_SIZE_XAUUSD
    lot = risk_usd / (sl_pips * PIP_VALUE_PER_LOT)
    return max(MIN_LOT, min(MAX_LOT, round(lot, 2)))


# ---------------------------------------------------------------------------
# Trade record
# ---------------------------------------------------------------------------

@dataclass
class Trade:
    entry_ts: datetime
    exit_ts: Optional[datetime]
    direction: str          # "BUY" ou "SELL"
    entry_price: float
    sl: float
    tp: float
    lot: float
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None   # "TP", "SL", "END"
    pnl_usd: float = 0.0
    session: str = "?"


# ---------------------------------------------------------------------------
# Simulation
# ---------------------------------------------------------------------------

@dataclass
class BacktestResult:
    strategy_name: str
    params: dict
    n_trades: int
    n_wins: int
    n_losses: int
    win_rate: float
    profit_factor: float
    total_pnl_usd: float
    max_drawdown_usd: float
    avg_win_usd: float
    avg_loss_usd: float
    avg_trade_duration_min: float
    trades_by_session: dict
    trades: list = field(default_factory=list)
    equity_curve: list = field(default_factory=list)  # [(ts_iso, equity_usd), ...]


def _session_of_hour(hour: int) -> str:
    """Retourne 'overlap', 'london', 'ny' ou 'off' selon l'heure UTC."""
    in_london = config.LONDON_START_HOUR <= hour < config.LONDON_END_HOUR
    in_ny = config.NY_START_HOUR <= hour < config.NY_END_HOUR
    if in_london and in_ny:
        return "overlap"
    if in_london:
        return "london"
    if in_ny:
        return "ny"
    return "off"


def run_backtest(
    strategy: BaseStrategy,
    df_m5: pl.DataFrame,
    df_h1: pl.DataFrame,
    daily_loss_max: float = None,
    commission_per_lot: float = None,
    slippage_ticks: float = None,
) -> BacktestResult:
    """Exécute le backtest. Retourne un BacktestResult."""
    if daily_loss_max is None:
        daily_loss_max = config.DAILY_LOSS_MAX_DOLLARS
    if commission_per_lot is None:
        commission_per_lot = config.COMMISSION_PER_LOT_USD
    if slippage_ticks is None:
        slippage_ticks = config.SLIPPAGE_TICKS

    df = strategy.compute_signals(df_m5, df_h1)

    # Extraire arrays pour la vitesse
    ts_arr = df["ts"].to_list()
    o = df["open"].to_numpy()
    h = df["high"].to_numpy()
    l = df["low"].to_numpy()
    c = df["close"].to_numpy()
    spr = df["spread_avg"].to_numpy() if "spread_avg" in df.columns else np.zeros(len(df))
    sig = df["signal"].to_list()
    sl_arr = df["sl"].to_numpy()
    tp_arr = df["tp"].to_numpy()

    n = len(df)
    trades: list[Trade] = []
    open_pos: Optional[Trade] = None
    current_day = None
    daily_pnl = 0.0
    equity = 0.0
    equity_curve = []
    peak_equity = 0.0
    max_dd = 0.0

    slip_price = slippage_ticks * config.XAUUSD_TICK_SIZE

    for i in range(n):
        ts = ts_arr[i]
        this_day = ts.date() if hasattr(ts, "date") else date.fromisoformat(str(ts)[:10])

        # Reset daily PnL à minuit UTC
        if current_day is None or this_day != current_day:
            current_day = this_day
            daily_pnl = 0.0

        # 1) Gérer la position ouverte : check SL / TP sur la bougie courante
        if open_pos is not None:
            hit_sl = False
            hit_tp = False
            if open_pos.direction == "BUY":
                if l[i] <= open_pos.sl:
                    hit_sl = True
                if h[i] >= open_pos.tp:
                    hit_tp = True
            else:  # SELL
                if h[i] >= open_pos.sl:
                    hit_sl = True
                if l[i] <= open_pos.tp:
                    hit_tp = True

            if hit_sl and hit_tp:
                # Cas ambigu : les deux touchés dans la même bougie
                # Convention conservatrice: on assume SL touché en premier
                exit_price, exit_reason = open_pos.sl, "SL"
            elif hit_sl:
                exit_price, exit_reason = open_pos.sl, "SL"
            elif hit_tp:
                exit_price, exit_reason = open_pos.tp, "TP"
            else:
                exit_price, exit_reason = None, None

            if exit_price is not None:
                # Slippage : mauvais pour nous (SL plus profond, TP moins loin)
                if open_pos.direction == "BUY":
                    if exit_reason == "SL":
                        exit_price -= slip_price
                    else:
                        exit_price -= slip_price
                    price_move = exit_price - open_pos.entry_price
                else:
                    if exit_reason == "SL":
                        exit_price += slip_price
                    else:
                        exit_price += slip_price
                    price_move = open_pos.entry_price - exit_price

                pnl = (price_move / PIP_SIZE_XAUUSD) * PIP_VALUE_PER_LOT * open_pos.lot
                # Commission aller-retour
                pnl -= commission_per_lot * open_pos.lot

                open_pos.exit_ts = ts
                open_pos.exit_price = exit_price
                open_pos.exit_reason = exit_reason
                open_pos.pnl_usd = round(pnl, 2)
                trades.append(open_pos)

                equity += pnl
                daily_pnl += pnl
                equity_curve.append((str(ts), round(equity, 2)))

                if equity > peak_equity:
                    peak_equity = equity
                dd = peak_equity - equity
                if dd > max_dd:
                    max_dd = dd

                open_pos = None

        # 2) Ouvrir une nouvelle position ?
        if open_pos is not None:
            continue
        if sig[i] == "HOLD":
            continue
        # Kill switch : pas d'entrée si le budget de perte du jour est atteint
        if -daily_pnl >= daily_loss_max:
            continue
        # SL / TP doivent être définis
        if np.isnan(sl_arr[i]) or np.isnan(tp_arr[i]):
            continue

        # Entry: close de la bougie + demi-spread + slippage
        entry_close = c[i]
        half_spread = spr[i] / 2.0
        if sig[i] == "BUY":
            entry_price = entry_close + half_spread + slip_price
            sl_distance = entry_price - sl_arr[i]
        else:
            entry_price = entry_close - half_spread - slip_price
            sl_distance = sl_arr[i] - entry_price

        if sl_distance <= 0:
            continue  # SL du mauvais côté, on skip

        # R:R minimum
        if sig[i] == "BUY":
            tp_distance = tp_arr[i] - entry_price
        else:
            tp_distance = entry_price - tp_arr[i]
        if tp_distance <= 0:
            continue
        rr = tp_distance / sl_distance
        if rr < config.MIN_RISK_REWARD:
            continue

        lot = compute_lot_size(RISK_PER_TRADE_USD, sl_distance)
        if lot < MIN_LOT:
            continue

        h_hour = ts.hour if hasattr(ts, "hour") else 0
        open_pos = Trade(
            entry_ts=ts,
            exit_ts=None,
            direction=sig[i],
            entry_price=round(entry_price, 3),
            sl=round(sl_arr[i], 3),
            tp=round(tp_arr[i], 3),
            lot=lot,
            session=_session_of_hour(h_hour),
        )

    # Fermeture forcée en fin de dataset (si position toujours ouverte)
    if open_pos is not None:
        exit_price = c[-1]
        if open_pos.direction == "BUY":
            price_move = exit_price - open_pos.entry_price
        else:
            price_move = open_pos.entry_price - exit_price
        pnl = (price_move / PIP_SIZE_XAUUSD) * PIP_VALUE_PER_LOT * open_pos.lot
        pnl -= commission_per_lot * open_pos.lot
        open_pos.exit_ts = ts_arr[-1]
        open_pos.exit_price = round(exit_price, 3)
        open_pos.exit_reason = "END"
        open_pos.pnl_usd = round(pnl, 2)
        trades.append(open_pos)
        equity += pnl
        equity_curve.append((str(ts_arr[-1]), round(equity, 2)))

    # --- Stats ---
    n_trades = len(trades)
    wins = [t for t in trades if t.pnl_usd > 0]
    losses = [t for t in trades if t.pnl_usd <= 0]
    n_wins = len(wins)
    n_losses = len(losses)
    win_rate = n_wins / n_trades if n_trades else 0.0
    total_wins = sum(t.pnl_usd for t in wins)
    total_losses = sum(t.pnl_usd for t in losses)
    profit_factor = (total_wins / abs(total_losses)) if total_losses != 0 else float("inf") if total_wins > 0 else 0.0
    total_pnl = sum(t.pnl_usd for t in trades)

    durations = []
    for t in trades:
        if t.exit_ts and t.entry_ts:
            dur_s = (t.exit_ts - t.entry_ts).total_seconds()
            durations.append(dur_s / 60)
    avg_dur = float(np.mean(durations)) if durations else 0.0

    by_session = {"london": 0, "ny": 0, "overlap": 0, "off": 0}
    for t in trades:
        by_session[t.session] = by_session.get(t.session, 0) + 1

    return BacktestResult(
        strategy_name=strategy.cfg.name,
        params=asdict(strategy.cfg),
        n_trades=n_trades,
        n_wins=n_wins,
        n_losses=n_losses,
        win_rate=round(win_rate, 3),
        profit_factor=round(profit_factor, 3) if profit_factor != float("inf") else -1.0,
        total_pnl_usd=round(total_pnl, 2),
        max_drawdown_usd=round(max_dd, 2),
        avg_win_usd=round(float(np.mean([t.pnl_usd for t in wins])) if wins else 0.0, 2),
        avg_loss_usd=round(float(np.mean([t.pnl_usd for t in losses])) if losses else 0.0, 2),
        avg_trade_duration_min=round(avg_dur, 1),
        trades_by_session=by_session,
        trades=[{
            "entry_ts": str(t.entry_ts),
            "exit_ts": str(t.exit_ts),
            "direction": t.direction,
            "entry": t.entry_price,
            "exit": t.exit_price,
            "sl": t.sl, "tp": t.tp,
            "lot": t.lot,
            "reason": t.exit_reason,
            "pnl": t.pnl_usd,
            "session": t.session,
        } for t in trades],
        equity_curve=equity_curve,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

STRATEGY_FACTORY = {
    "supertrend_atr": (SupertrendAtrStrategy, SupertrendAtrConfig),
    "hull_vwap": (HullVwapStrategy, HullVwapConfig),
    "ttm_adx": (TtmAdxStrategy, TtmAdxConfig),
}


def _parse_params(pairs: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for p in pairs:
        if "=" not in p:
            continue
        k, v = p.split("=", 1)
        try:
            out[k] = int(v)
        except ValueError:
            try:
                out[k] = float(v)
            except ValueError:
                out[k] = v
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--strategy", required=True, choices=list(STRATEGY_FACTORY.keys()))
    parser.add_argument("--dataset", default="train", choices=["train", "quarantine"])
    parser.add_argument("--params", nargs="*", default=[], help="key=value overrides")
    args = parser.parse_args()

    # Charger les données
    if args.dataset == "train":
        m5_path = config.DATA_DIR / "xauusd_m5.parquet"
        h1_path = config.DATA_DIR / "xauusd_h1.parquet"
    else:
        m5_path = config.QUARANTINE_DIR / "xauusd_m5.parquet"
        h1_path = config.QUARANTINE_DIR / "xauusd_h1.parquet"

    if not m5_path.exists():
        logger.error("Fichier %s introuvable. Lance d'abord data_collector.", m5_path)
        sys.exit(1)

    df_m5 = pl.read_parquet(m5_path).sort("ts")
    df_h1 = pl.read_parquet(h1_path).sort("ts")

    # S'assurer que ts est en datetime avec TZ UTC
    if df_m5["ts"].dtype != pl.Datetime(time_zone="UTC"):
        df_m5 = df_m5.with_columns(pl.col("ts").cast(pl.Datetime("us", "UTC")))
    if df_h1["ts"].dtype != pl.Datetime(time_zone="UTC"):
        df_h1 = df_h1.with_columns(pl.col("ts").cast(pl.Datetime("us", "UTC")))

    logger.info("M5 bars: %d — H1 bars: %d", df_m5.height, df_h1.height)

    # Construire la stratégie
    strat_cls, cfg_cls = STRATEGY_FACTORY[args.strategy]
    overrides = _parse_params(args.params)
    cfg_kwargs = {"name": args.strategy}
    cfg_kwargs.update(overrides)
    cfg = cfg_cls(**cfg_kwargs)
    strategy = strat_cls(cfg)

    logger.info("Running strategy: %s with %s", args.strategy, cfg)
    result = run_backtest(strategy, df_m5, df_h1)

    # Écrire résultats
    out_json = config.REPORTS_DIR / f"backtest_{args.strategy}_{args.dataset}.json"
    out_json.write_text(json.dumps(asdict(result), indent=2, default=str))
    logger.info("JSON written: %s", out_json)

    # Écrire rapport HTML
    from src.report import render_report
    out_html = config.REPORTS_DIR / f"backtest_{args.strategy}_{args.dataset}.html"
    render_report(result, out_html)
    logger.info("HTML written: %s", out_html)

    # Résumé console
    print("\n" + "=" * 60)
    print(f"STRATEGY : {result.strategy_name}   DATASET : {args.dataset}")
    print("=" * 60)
    print(f"Trades           : {result.n_trades}")
    print(f"Win rate         : {result.win_rate:.1%}")
    print(f"Profit factor    : {result.profit_factor}")
    print(f"Total PnL        : {result.total_pnl_usd} $")
    print(f"Max drawdown     : {result.max_drawdown_usd} $")
    print(f"Avg trade dur.   : {result.avg_trade_duration_min} min")
    print(f"By session       : {result.trades_by_session}")
    print("=" * 60)


if __name__ == "__main__":
    main()
