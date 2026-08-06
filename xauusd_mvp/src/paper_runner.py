"""
paper_runner.py

Boucle live pour supertrend_atr sur MetaApi (compte demo XM).

Trois modes:
  - dry   : calcule signaux, journalise, aucun ordre (defaut)
  - paper : envoie ordres au compte configure (doit etre demo)
  - live  : idem paper mais refuse si LIVE_ENABLED=false
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal as os_signal
import sys
import traceback
from dataclasses import dataclass, field, asdict
from datetime import datetime, date, time, timedelta, timezone
from pathlib import Path
from typing import Optional

import httpx
import numpy as np
import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402

from src.strategies.supertrend_atr import SupertrendAtrStrategy, SupertrendAtrConfig
from src.metaapi_connector import (
    get_config as metaapi_get_config,
    get_pricing, get_account_summary, get_open_trades,
    place_market_order, close_trade, calculate_units,
    MetaApiError, MetaApiConfigError,
)
from src.shared import (
    telegram_send as shared_telegram, journal_append as shared_journal_append,
    is_kill_switch_active, kill_switch_message,
)

LOG_PATH = config.LOGS_DIR / "paper_runner.log"
ERROR_LOG_PATH = config.LOGS_DIR / "errors.jsonl"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_PATH),
    ],
)
logger = logging.getLogger("paper_runner")


STRATEGY_CFG = SupertrendAtrConfig(
    name="supertrend_atr",
    st_length=10,
    st_factor=3.0,
    atr_length=14,
    tp_k=5.0,
    h1_atr_min=15.0,
    time_exit_bars=24,
)

RISK_PER_TRADE_USD = 10.0
LOOP_TICK_SECONDS = 30
BAR_ALIGNMENT_MINUTES = 5


def telegram_notify(message: str) -> None:
    """Wrapper vers shared.telegram_send."""
    shared_telegram(message)


def journal_append(kind: str, data: dict) -> None:
    """Wrapper vers shared.journal_append avec source='scalp'."""
    entry = {"kind": kind, "source": "scalp", **data}
    shared_journal_append(entry)


def _fetch_candles_with_time_vol(tf: str, count: int) -> list[dict]:
    """Appel MetaApi direct pour recuperer time + tickVolume avec OHLC."""
    cfg = metaapi_get_config()
    granularity_map = {"M5": "5m", "H1": "1h"}
    if tf not in granularity_map:
        raise ValueError(f"tf unsupported: {tf}")

    host = f"https://mt-market-data-client-api-v1.{cfg['region']}.agiliumtrade.ai"
    url = (
        f"{host}/users/current/accounts/{cfg['account_id']}"
        f"/historical-market-data/symbols/{cfg['symbol']}"
        f"/timeframes/{granularity_map[tf]}/candles"
    )
    headers = {"auth-token": cfg["token"], "accept": "application/json"}
    with httpx.Client(timeout=30) as client:
        r = client.get(url, headers=headers, params={"limit": count})
        r.raise_for_status()
        raw = r.json()
    raw_sorted = sorted(raw, key=lambda c: c.get("time", ""))
    out = []
    for c in raw_sorted:
        ts_str = c.get("time")
        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        out.append({
            "ts": ts,
            "open": float(c["open"]),
            "high": float(c["high"]),
            "low": float(c["low"]),
            "close": float(c["close"]),
            "tick_volume": float(c.get("tickVolume", 0) or 0),
            "spread_avg": float(c.get("spread", 0) or 0),
        })
    return out


def bars_to_polars(bars: list[dict]) -> pl.DataFrame:
    return pl.DataFrame({
        "ts": [b["ts"] for b in bars],
        "open": [b["open"] for b in bars],
        "high": [b["high"] for b in bars],
        "low": [b["low"] for b in bars],
        "close": [b["close"] for b in bars],
        "tick_volume": [b["tick_volume"] for b in bars],
        "spread_avg": [b["spread_avg"] for b in bars],
    }).with_columns(pl.col("ts").cast(pl.Datetime("us", "UTC")))


@dataclass
class OpenPosition:
    metaapi_id: str
    direction: str
    entry_price: float
    sl: float
    tp: float
    lot: float
    entry_ts: datetime
    bars_held: int = 0


@dataclass
class RunnerState:
    open_position: Optional[OpenPosition] = None
    equity_start_of_day: Optional[float] = None
    current_day: Optional[date] = None
    daily_pnl_cumulative: float = 0.0
    kill_switch_until_midnight: bool = False
    signals_journal: list = field(default_factory=list)


def log_error(where: str, exc: Exception) -> None:
    entry = {
        "ts_utc": datetime.now(timezone.utc).isoformat(),
        "where": where,
        "error": str(exc),
        "traceback": traceback.format_exc(),
    }
    with ERROR_LOG_PATH.open("a") as f:
        f.write(json.dumps(entry, default=str) + "\n")


async def sleep_until_next_m5_close() -> None:
    now = datetime.now(timezone.utc)
    minutes_to_next = BAR_ALIGNMENT_MINUTES - (now.minute % BAR_ALIGNMENT_MINUTES)
    next_close = now.replace(second=0, microsecond=0) + timedelta(minutes=minutes_to_next)
    wake = next_close + timedelta(seconds=5)
    wait = (wake - now).total_seconds()
    logger.info("Sleeping %.0fs until %s (next M5 close + 5s)", wait, wake.isoformat())
    await asyncio.sleep(max(wait, 1))


async def refresh_state_daily(state: RunnerState) -> None:
    today = datetime.now(timezone.utc).date()
    if state.current_day is None or today != state.current_day:
        try:
            summary = await asyncio.to_thread(get_account_summary)
            state.equity_start_of_day = summary["equite"]
        except Exception as e:
            logger.warning("Cannot fetch equity for daily reset: %s", e)
            state.equity_start_of_day = None
        state.current_day = today
        state.daily_pnl_cumulative = 0.0
        state.kill_switch_until_midnight = False


async def check_and_close_position_if_needed(state: RunnerState, mode: str) -> None:
    if state.open_position is None:
        return
    try:
        trades = await asyncio.to_thread(get_open_trades)
    except Exception as e:
        logger.warning("Cannot fetch open trades: %s", e)
        return

    open_ids = {t.get("id") for t in trades}
    if state.open_position.metaapi_id not in open_ids:
        journal_append("position_closed_by_broker", asdict(state.open_position))
        telegram_notify(
            f"Position fermee par broker (SL/TP)\n"
            f"Direction: {state.open_position.direction}\n"
            f"Entry: {state.open_position.entry_price}"
        )
        state.open_position = None
        return

    if state.open_position.bars_held >= STRATEGY_CFG.time_exit_bars:
        logger.info("TIME exit triggered (bars_held=%d)", state.open_position.bars_held)
        if mode in ("paper", "live"):
            try:
                await asyncio.to_thread(close_trade, state.open_position.metaapi_id)
                journal_append("position_closed_time_exit", asdict(state.open_position))
                telegram_notify(
                    f"TIME exit ({state.open_position.bars_held} bougies)\n"
                    f"Direction: {state.open_position.direction}"
                )
            except Exception as e:
                logger.error("Failed to close on TIME: %s", e)
                log_error("close_time_exit", e)
        else:
            journal_append("dry_time_exit", asdict(state.open_position))
        state.open_position = None


async def try_open_position(
    state: RunnerState,
    df_m5: pl.DataFrame,
    df_h1: pl.DataFrame,
    mode: str,
) -> None:
    strategy = SupertrendAtrStrategy(STRATEGY_CFG)
    enriched = strategy.compute_signals(df_m5, df_h1)

    last = enriched.tail(1)
    sig = last["signal"][0]
    sl = last["sl"][0]
    tp = last["tp"][0]
    close_price = last["close"][0]

    if sig == "HOLD" or sig is None:
        return
    if sl is None or tp is None or (isinstance(sl, float) and np.isnan(sl)):
        return
    if isinstance(tp, float) and np.isnan(tp):
        return

    if sig == "BUY":
        sl_distance = close_price - sl
        tp_distance = tp - close_price
    else:
        sl_distance = sl - close_price
        tp_distance = close_price - tp
    if sl_distance <= 0 or tp_distance <= 0:
        logger.warning("Invalid SL/TP: sig=%s close=%s sl=%s tp=%s", sig, close_price, sl, tp)
        return
    rr = tp_distance / sl_distance
    if rr < config.MIN_RISK_REWARD:
        logger.info("R:R %.2f < min %s, skipping", rr, config.MIN_RISK_REWARD)
        return

    try:
        pricing = await asyncio.to_thread(get_pricing)
        entry_price = pricing["ask"] if sig == "BUY" else pricing["bid"]
    except Exception as e:
        logger.error("Cannot fetch pricing: %s", e)
        log_error("get_pricing", e)
        return

    if sig == "BUY":
        sl_distance_real = entry_price - sl
    else:
        sl_distance_real = sl - entry_price
    if sl_distance_real <= 0:
        logger.warning("SL du mauvais cote apres entry reel, skip")
        return

    units = calculate_units(RISK_PER_TRADE_USD, sl_distance_real, sig, lot_size=100.0)
    if units <= 0:
        logger.warning("Units calcule = %s, skip", units)
        return

    if mode == "dry":
        logger.info("[DRY] Would open %s: entry=%s sl=%s tp=%s units=%s",
                    sig, entry_price, sl, tp, units)
        journal_append("dry_signal", {
            "direction": sig, "entry": entry_price, "sl": sl, "tp": tp, "units": units, "rr": rr,
        })
        return

    try:
        result = await asyncio.to_thread(
            place_market_order,
            direction=sig, units=units, sl=sl, tp=tp,
        )
        pos_id = result.get("orderId") or result.get("positionId") or result.get("id")
        if not pos_id:
            logger.error("No position id in response: %s", result)
            return

        state.open_position = OpenPosition(
            metaapi_id=str(pos_id),
            direction=sig,
            entry_price=float(entry_price),
            sl=float(sl),
            tp=float(tp),
            lot=float(units),
            entry_ts=datetime.now(timezone.utc),
        )
        journal_append("position_opened", asdict(state.open_position))

        arrow = "BUY" if sig == "BUY" else "SELL"
        pip_size = 0.1
        sl_pips = abs(entry_price - sl) / pip_size
        tp_pips = abs(tp - entry_price) / pip_size
        telegram_notify(
            f"SIGNAL {arrow} GOLD\n\n"
            f"Entry : `{entry_price:.2f}`\n"
            f"SL    : `{sl:.2f}`  (-{sl_pips:.0f} pips)\n"
            f"TP    : `{tp:.2f}`  (+{tp_pips:.0f} pips)\n"
            f"Lot   : `{units}`\n"
            f"R:R   : `{rr:.2f}`"
        )
    except Exception as e:
        logger.error("place_market_order failed: %s", e)
        log_error("place_market_order", e)
        telegram_notify(f"Ordre echoue: `{e}`")


async def loop_iteration(state: RunnerState, mode: str) -> None:
    await refresh_state_daily(state)

    if is_kill_switch_active():
        if not state.kill_switch_until_midnight:
            state.kill_switch_until_midnight = True
            logger.warning("KILL SWITCH partage active")
            telegram_notify(kill_switch_message())
        if state.open_position:
            state.open_position.bars_held += 1
            await check_and_close_position_if_needed(state, mode)
        return

    try:
        m5_raw = await asyncio.to_thread(_fetch_candles_with_time_vol, "M5", 500)
        h1_raw = await asyncio.to_thread(_fetch_candles_with_time_vol, "H1", 200)
    except Exception as e:
        logger.error("Cannot fetch candles: %s", e)
        log_error("fetch_candles", e)
        return

    df_m5 = bars_to_polars(m5_raw)
    df_h1 = bars_to_polars(h1_raw)

    if state.open_position is not None:
        state.open_position.bars_held += 1
        await check_and_close_position_if_needed(state, mode)

    if state.open_position is None:
        await try_open_position(state, df_m5, df_h1, mode)


async def main_loop(mode: str) -> None:
    logger.info("Starting paper_runner in mode=%s", mode)
    logger.info("Strategy config: %s", STRATEGY_CFG)
    logger.info("Kill switch: %s $/day", config.DAILY_LOSS_MAX_DOLLARS)

    state = RunnerState()

    try:
        summary = await asyncio.to_thread(get_account_summary)
        state.equity_start_of_day = summary["equite"]
        state.current_day = datetime.now(timezone.utc).date()
        logger.info("Equity de depart: %s", state.equity_start_of_day)
    except Exception as e:
        logger.error("Cannot fetch initial account: %s", e)
        log_error("initial_account", e)

    # --- Message de demarrage enrichi ---
    try:
        pricing_start = await asyncio.to_thread(get_pricing)
        mid_price = (pricing_start["bid"] + pricing_start["ask"]) / 2
        spread_now = pricing_start["ask"] - pricing_start["bid"]
    except Exception:
        pricing_start = None
        mid_price = None
        spread_now = None

    now_utc = datetime.now(timezone.utc)
    h = now_utc.hour
    in_london = config.LONDON_START_HOUR <= h < config.LONDON_END_HOUR
    in_ny = config.NY_START_HOUR <= h < config.NY_END_HOUR
    if in_london and in_ny:
        session_label = "Overlap Londres/NY (fenetre la plus liquide)"
    elif in_london:
        session_label = "Londres"
    elif in_ny:
        session_label = "New York"
    else:
        session_label = "Hors session (aucune entree autorisee)"

    try:
        h1_raw = await asyncio.to_thread(_fetch_candles_with_time_vol, "H1", 100)
        df_h1_boot = bars_to_polars(h1_raw)
        from src.indicators.atr import atr as _atr_boot
        atr_h1_series = _atr_boot(
            df_h1_boot["high"].to_numpy(),
            df_h1_boot["low"].to_numpy(),
            df_h1_boot["close"].to_numpy(),
            14,
        )
        atr_h1_now = float(atr_h1_series[-1])
    except Exception:
        atr_h1_now = None

    bs = config.BACKTEST_WEEK_STATS

    lines = []
    lines.append("Systeme actif")
    lines.append("")
    lines.append("*Compte*")
    lines.append(f"  Capital de depart : `{config.INITIAL_CAPITAL_USD:.2f} $`")
    if state.equity_start_of_day:
        lines.append(f"  Equite courante   : `{state.equity_start_of_day:.2f} $`")
    else:
        lines.append("  Equite courante   : indisponible")
    lines.append(f"  Kill switch       : `-{config.DAILY_LOSS_MAX_DOLLARS:.0f} $ / jour`")
    lines.append("")
    lines.append("*Marche GOLD*")
    if pricing_start:
        lines.append(f"  Prix mid    : `{mid_price:.2f} $`")
        lines.append(f"  Bid / Ask   : `{pricing_start['bid']:.2f} / {pricing_start['ask']:.2f}`")
        lines.append(f"  Spread live : `{spread_now:.2f} $`")
    else:
        lines.append("  Prix indisponible au demarrage")
    if atr_h1_now is not None:
        lines.append(f"  ATR H1      : `{atr_h1_now:.2f} $`  (volatilite horaire)")
    lines.append("")
    lines.append("*Session*")
    lines.append(f"  {session_label}")
    lines.append("  Fenetres tradees : Londres 07-16 / NY 13-22 UTC")
    lines.append("")
    lines.append("*Strategie*")
    lines.append("  Nom      : supertrend trend-following")
    lines.append("  Entree   : M5   Contexte : H1")
    lines.append(f"  Filtre   : ATR H1 minimum `{STRATEGY_CFG.h1_atr_min:.0f} $`")
    lines.append("  SL       : niveau SuperTrend au flip")
    lines.append(f"  TP       : `{STRATEGY_CFG.tp_k:.1f}x ATR M5`")
    lines.append(f"  Time-exit: `{STRATEGY_CFG.time_exit_bars} bougies (2h)`")
    lines.append("  Risque   : `10 $ / trade`  |  RR min : `1.5`")
    lines.append("")
    lines.append("*Vision du jour*")
    if atr_h1_now is not None:
        expected_moves = atr_h1_now * 14
        lines.append(f"  Amplitude attendue : ~`{expected_moves:.0f} $` sur la journee")
        if atr_h1_now < STRATEGY_CFG.h1_atr_min:
            lines.append(f"  Volatilite H1 sous le seuil `{STRATEGY_CFG.h1_atr_min:.0f}$` -> peu de signaux")
        elif atr_h1_now > 25:
            lines.append("  Volatilite elevee -> vigilance")
        else:
            lines.append("  Volatilite normale -> conditions standard")
    lines.append("  Signaux attendus : ~3 a 5 flips SuperTrend par jour complet")
    lines.append("")
    lines.append("*Backtesting week strategie*")
    lines.append(f"  PF train        : `{bs['profit_factor_train']}`")
    lines.append(f"  PF quarantaine  : `{bs['profit_factor_quarantine']}`")
    lines.append(f"  Trades observes : `{bs['n_trades_train']} + {bs['n_trades_quarantine']}`")

    telegram_notify("\n".join(lines))

    while True:
        try:
            await sleep_until_next_m5_close()
            await loop_iteration(state, mode)
        except asyncio.CancelledError:
            logger.info("Cancelled, shutting down")
            break
        except Exception as e:
            logger.error("Loop error: %s", e)
            log_error("loop", e)
            telegram_notify(f"Loop error: `{e}`")
            await asyncio.sleep(30)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode", choices=["dry", "paper", "live"], default="dry",
    )
    args = parser.parse_args()

    if args.mode == "live":
        if not config.LIVE_ENABLED:
            print("Mode 'live' demande mais LIVE_ENABLED=false dans .env.")
            sys.exit(1)
        confirm = input("Mode LIVE va envoyer des ordres REELS. Taper 'CONFIRMER' pour continuer: ")
        if confirm != "CONFIRMER":
            print("Abandon.")
            sys.exit(0)

    try:
        cfg = metaapi_get_config()
        logger.info("MetaApi config OK - account_id=%s region=%s symbol=%s",
                    cfg["account_id"][:8] + "...", cfg["region"], cfg["symbol"])
    except MetaApiConfigError as e:
        print(f"Config MetaApi invalide: {e}")
        sys.exit(1)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def shutdown():
        logger.info("Shutdown requested")
        for task in asyncio.all_tasks(loop):
            task.cancel()

    for sig in (os_signal.SIGINT, os_signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, shutdown)
        except NotImplementedError:
            pass

    try:
        loop.run_until_complete(main_loop(args.mode))
    finally:
        loop.close()


if __name__ == "__main__":
    main()