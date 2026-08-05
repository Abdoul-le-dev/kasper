"""
paper_runner.py

Boucle live pour tester supertrend_atr sur MetaApi (compte démo XM).

Trois modes d'exécution (via --mode) :
  - dry   : calcule les signaux, journalise, PAS d'ordres envoyés (défaut, safe)
  - paper : envoie de vrais ordres au compte METAAPI_ACCOUNT_ID (qui DOIT être un démo)
  - live  : idem paper mais refusé si LIVE_ENABLED=false dans .env

Sécurités actives dans tous les modes :
  - Une seule position à la fois
  - Kill switch : perte cumulée journalière ≥ 50 $ → HOLD jusqu'à minuit UTC
  - Time-exit après 24 bougies M5 (2h)
  - Session Londres / NY / overlap uniquement
  - R:R minimum 1.5
  - Notification Telegram à chaque événement

Usage:
    python -m src.paper_runner --mode dry     # simulation, aucun ordre
    python -m src.paper_runner --mode paper   # ordres réels sur compte démo
    python -m src.paper_runner --mode live    # ordres réels sur compte réel (LIVE_ENABLED=true requis)
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

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

LOG_PATH = config.LOGS_DIR / "paper_runner.log"
JOURNAL_PATH = config.LOGS_DIR / "paper_trades.jsonl"
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


# ---------------------------------------------------------------------------
# Constantes stratégie (FIGÉES après Phase 2)
# ---------------------------------------------------------------------------

STRATEGY_CFG = SupertrendAtrConfig(
    name="supertrend_atr",
    st_length=10,
    st_factor=3.0,
    atr_length=14,
    tp_k=5.0,
    h1_atr_min=15.0,
    time_exit_bars=24,
)

RISK_PER_TRADE_USD = 10.0   # 5 pertes = 50 $ = kill switch
LOOP_TICK_SECONDS = 30       # check position status toutes les 30s
BAR_ALIGNMENT_MINUTES = 5    # M5 alignée sur clock UTC


# ---------------------------------------------------------------------------
# Telegram (léger, sync via httpx)
# ---------------------------------------------------------------------------

def telegram_notify(message: str) -> None:
    """Envoi Telegram best-effort. N'échoue jamais."""
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
        with httpx.Client(timeout=10) as client:
            client.post(url, json={
                "chat_id": config.TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "Markdown",
            })
    except Exception as e:
        logger.warning("Telegram failed: %s", e)


# ---------------------------------------------------------------------------
# Fetching candles avec time + volume (bypass du _parse_candle du connector qui perd l'info)
# ---------------------------------------------------------------------------

def _fetch_candles_with_time_vol(tf: str, count: int) -> list[dict]:
    """Appel MetaApi direct pour récupérer time + tickVolume en plus de OHLC."""
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
        ts_str = c.get("time")  # ISO 8601
        # MetaApi renvoie parfois "2024-...+00:00" ou "Z"
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


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

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
    daily_pnl_cumulative: float = 0.0  # tracked via account summary
    kill_switch_until_midnight: bool = False
    signals_journal: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# Journal
# ---------------------------------------------------------------------------

def journal_append(kind: str, data: dict) -> None:
    entry = {
        "ts_utc": datetime.now(timezone.utc).isoformat(),
        "kind": kind,
        **data,
    }
    with JOURNAL_PATH.open("a") as f:
        f.write(json.dumps(entry, default=str) + "\n")


def log_error(where: str, exc: Exception) -> None:
    entry = {
        "ts_utc": datetime.now(timezone.utc).isoformat(),
        "where": where,
        "error": str(exc),
        "traceback": traceback.format_exc(),
    }
    with ERROR_LOG_PATH.open("a") as f:
        f.write(json.dumps(entry, default=str) + "\n")


# ---------------------------------------------------------------------------
# Boucle principale
# ---------------------------------------------------------------------------

async def sleep_until_next_m5_close() -> None:
    """Attend jusqu'à la prochaine clôture de bougie M5 (aligné sur minutes % 5 == 0),
    +5 secondes de marge pour laisser le broker publier la bougie."""
    now = datetime.now(timezone.utc)
    minutes_to_next = BAR_ALIGNMENT_MINUTES - (now.minute % BAR_ALIGNMENT_MINUTES)
    next_close = now.replace(second=0, microsecond=0) + timedelta(minutes=minutes_to_next)
    # +5s de marge pour que la bougie fermée soit disponible côté broker
    wake = next_close + timedelta(seconds=5)
    wait = (wake - now).total_seconds()
    logger.info("Sleeping %.0fs until %s (next M5 close + 5s)", wait, wake.isoformat())
    await asyncio.sleep(max(wait, 1))


async def refresh_state_daily(state: RunnerState) -> None:
    """Reset daily PnL et kill switch à minuit UTC."""
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
        telegram_notify(f"🌅 New trading day — equity start: {state.equity_start_of_day}")


async def compute_daily_pnl(state: RunnerState) -> float:
    """Retourne la perte cumulée du jour (positive si perte)."""
    if state.equity_start_of_day is None:
        return 0.0
    try:
        summary = await asyncio.to_thread(get_account_summary)
        pnl = summary["equite"] - state.equity_start_of_day
        return -pnl  # positive = perte
    except Exception as e:
        logger.warning("Cannot compute daily PnL: %s", e)
        return 0.0


async def check_and_close_position_if_needed(state: RunnerState, mode: str) -> None:
    """Vérifie SL/TP côté broker (fermé automatiquement) et applique TIME-exit."""
    if state.open_position is None:
        return

    # Vérifier si toujours ouverte côté broker
    try:
        trades = await asyncio.to_thread(get_open_trades)
    except Exception as e:
        logger.warning("Cannot fetch open trades: %s", e)
        return

    open_ids = {t.get("id") for t in trades}
    if state.open_position.metaapi_id not in open_ids:
        # Position fermée par le broker (SL ou TP)
        journal_append("position_closed_by_broker", asdict(state.open_position))
        telegram_notify(
            f"📤 Position fermée par broker (SL/TP)\n"
            f"Direction: {state.open_position.direction}\n"
            f"Entry: {state.open_position.entry_price}"
        )
        state.open_position = None
        return

    # TIME exit
    if state.open_position.bars_held >= STRATEGY_CFG.time_exit_bars:
        logger.info("TIME exit triggered (bars_held=%d)", state.open_position.bars_held)
        if mode in ("paper", "live"):
            try:
                await asyncio.to_thread(close_trade, state.open_position.metaapi_id)
                journal_append("position_closed_time_exit", asdict(state.open_position))
                telegram_notify(
                    f"⏰ TIME exit ({state.open_position.bars_held} bougies)\n"
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
    """Calcule les signaux, ouvre une position si conditions réunies."""
    strategy = SupertrendAtrStrategy(STRATEGY_CFG)
    enriched = strategy.compute_signals(df_m5, df_h1)

    # On regarde le signal de la DERNIÈRE bougie fermée
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

    # R:R check
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

    # Sizing
    try:
        pricing = await asyncio.to_thread(get_pricing)
        # Entry réel : ask pour BUY, bid pour SELL
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
        logger.warning("SL du mauvais côté après entry réel, skip")
        return

    units = calculate_units(RISK_PER_TRADE_USD, sl_distance_real, sig, lot_size=100.0)
    if units <= 0:
        logger.warning("Units calculé = %s, skip", units)
        return

    # --- Envoi de l'ordre ---
    if mode == "dry":
        logger.info("[DRY] Would open %s: entry=%s sl=%s tp=%s units=%s",
                    sig, entry_price, sl, tp, units)
        journal_append("dry_signal", {
            "direction": sig, "entry": entry_price, "sl": sl, "tp": tp, "units": units, "rr": rr,
        })
        telegram_notify(
            f"🔵 [DRY] Signal {sig}\n"
            f"Entry: {entry_price}\n"
            f"SL: {sl:.2f}  TP: {tp:.2f}\n"
            f"R:R: {rr:.2f}"
        )
        return

    # mode paper ou live : vraie ouverture
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
            lot=units / 100.0,
            entry_ts=datetime.now(timezone.utc),
        )
        journal_append("position_opened", asdict(state.open_position))
        telegram_notify(
            f"✅ Position ouverte {sig}\n"
            f"Entry: {entry_price}\n"
            f"SL: {sl:.2f}  TP: {tp:.2f}\n"
            f"Lot: {units/100.0:.2f}  R:R: {rr:.2f}"
        )
    except Exception as e:
        logger.error("place_market_order failed: %s", e)
        log_error("place_market_order", e)
        telegram_notify(f"⚠️ Ordre échoué: {e}")


async def loop_iteration(state: RunnerState, mode: str) -> None:
    """Une itération complète : refresh, signaux, gestion position."""
    await refresh_state_daily(state)

    # Kill switch check
    daily_loss = await compute_daily_pnl(state)
    if daily_loss >= config.DAILY_LOSS_MAX_DOLLARS:
        if not state.kill_switch_until_midnight:
            state.kill_switch_until_midnight = True
            logger.warning("🛑 KILL SWITCH — perte %s $ ≥ %s $", daily_loss, config.DAILY_LOSS_MAX_DOLLARS)
            telegram_notify(
                f"🛑 KILL SWITCH activé\n"
                f"Perte du jour: {daily_loss:.2f} $ ≥ {config.DAILY_LOSS_MAX_DOLLARS} $\n"
                f"HOLD jusqu'à minuit UTC"
            )
        # On peut toujours gérer les positions ouvertes, mais pas en ouvrir de nouvelles
        if state.open_position:
            state.open_position.bars_held += 1
            await check_and_close_position_if_needed(state, mode)
        return

    # Fetch données à jour
    try:
        m5_raw = await asyncio.to_thread(_fetch_candles_with_time_vol, "M5", 500)
        h1_raw = await asyncio.to_thread(_fetch_candles_with_time_vol, "H1", 200)
    except Exception as e:
        logger.error("Cannot fetch candles: %s", e)
        log_error("fetch_candles", e)
        return

    df_m5 = bars_to_polars(m5_raw)
    df_h1 = bars_to_polars(h1_raw)

    # Gestion position existante (incrémente bars_held, check TIME/SL/TP)
    if state.open_position is not None:
        state.open_position.bars_held += 1
        await check_and_close_position_if_needed(state, mode)

    # Ouvrir une nouvelle position ?
    if state.open_position is None:
        await try_open_position(state, df_m5, df_h1, mode)


async def main_loop(mode: str) -> None:
    logger.info("Starting paper_runner in mode=%s", mode)
    logger.info("Strategy config: %s", STRATEGY_CFG)
    logger.info("Kill switch: %s $/day", config.DAILY_LOSS_MAX_DOLLARS)
    telegram_notify(
        f"🚀 paper_runner démarré (mode={mode})\n"
        f"Stratégie: supertrend_atr\n"
        f"Kill switch: {config.DAILY_LOSS_MAX_DOLLARS}$/jour"
    )

    state = RunnerState()

    # Warmup équité de départ
    try:
        summary = await asyncio.to_thread(get_account_summary)
        state.equity_start_of_day = summary["equite"]
        state.current_day = datetime.now(timezone.utc).date()
        logger.info("Equity de départ: %s", state.equity_start_of_day)
    except Exception as e:
        logger.error("Cannot fetch initial account: %s", e)
        log_error("initial_account", e)

    # Boucle principale
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
            telegram_notify(f"⚠️ Loop error: {e}")
            await asyncio.sleep(30)


# ---------------------------------------------------------------------------
# CLI + safety
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode", choices=["dry", "paper", "live"], default="dry",
        help="dry=simulation locale, paper=ordres sur compte configuré (démo), live=idem paper (config real, LIVE_ENABLED required)"
    )
    args = parser.parse_args()

    # Sécurité live
    if args.mode == "live":
        if not config.LIVE_ENABLED:
            print("❌ Mode 'live' demandé mais LIVE_ENABLED=false dans .env.")
            print("   Pour autoriser le live : mettre LIVE_ENABLED=true dans .env explicitement.")
            sys.exit(1)
        confirm = input("⚠️  Mode LIVE va envoyer des ordres RÉELS. Taper 'CONFIRMER' pour continuer: ")
        if confirm != "CONFIRMER":
            print("Abandon.")
            sys.exit(0)

    # Config check MetaApi
    try:
        cfg = metaapi_get_config()
        logger.info("MetaApi config OK — account_id=%s region=%s symbol=%s",
                    cfg["account_id"][:8] + "...", cfg["region"], cfg["symbol"])
    except MetaApiConfigError as e:
        print(f"❌ Config MetaApi invalide: {e}")
        sys.exit(1)

    # Signal handler pour Ctrl+C propre
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
            pass  # Windows

    try:
        loop.run_until_complete(main_loop(args.mode))
    finally:
        loop.close()
        telegram_notify("👋 paper_runner arrêté")


if __name__ == "__main__":
    main()
