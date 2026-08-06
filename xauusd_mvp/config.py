"""
config.py

Constantes globales du système. Un seul fichier, lu partout.
Toute valeur figée par la spec est en dur ici, non modifiable en runtime.
"""

import os
from pathlib import Path
from datetime import date, timedelta

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # dotenv optionnel, les vars peuvent aussi venir de l'environnement système

# --- Chemins ---
ROOT = Path(__file__).parent
DATA_DIR = ROOT / "data"
QUARANTINE_DIR = DATA_DIR / "quarantine"
REPORTS_DIR = ROOT / "reports"
LOGS_DIR = ROOT / "logs"

for d in (DATA_DIR, QUARANTINE_DIR, REPORTS_DIR, LOGS_DIR):
    d.mkdir(parents=True, exist_ok=True)

# --- Actif ---
SYMBOL = "XAUUSD"
DUKASCOPY_INSTRUMENT = "XAUUSD"  # code Dukascopy
BROKER_SYMBOL = os.environ.get("METAAPI_SYMBOL", "XAUUSD")

# --- Fenêtre historique ---
# 12 mois glissants jusqu'à hier UTC. Les 2 derniers mois vont en quarantaine.
TODAY = date.today()
HISTORY_END = TODAY - timedelta(days=1)          # hier
HISTORY_START = HISTORY_END - timedelta(days=365) # -12 mois
QUARANTINE_START = HISTORY_END - timedelta(days=60)  # 2 derniers mois

# --- Timeframes ---
TIMEFRAMES = ["M1", "M5", "M15", "M30", "H1"]
TIMEFRAME_MINUTES = {"M1": 1, "M5": 5, "M15": 15, "M30": 30, "H1": 60}

# --- Sessions (UTC, sans DST) ---
LONDON_START_HOUR = 7
LONDON_END_HOUR = 16
NY_START_HOUR = 13
NY_END_HOUR = 22
# Overlap = intersection = 13-16 UTC

TRADING_SESSIONS = {"london", "new_york", "overlap_london_ny"}

# --- Risque (kill switch dur) ---
INITIAL_CAPITAL_USD = 100.0
DAILY_LOSS_MAX_DOLLARS = float(os.environ.get("DAILY_LOSS_MAX_DOLLARS", "30"))
MAX_OPEN_POSITIONS = 1  # par système : le scalp OU l'ordre direct, chacun peut en avoir 1
MIN_RISK_REWARD = 1.5

MANUAL_PRICE_TOLERANCE_USD = 5.0  # écart max autorisé entre prix tapé et prix marché

BACKTEST_WEEK_STATS = {
    "strategy": "supertrend_atr",
    "params": "st_length=10, st_factor=3, tp_k=5, h1_atr_min=15, time_exit=24",
    "profit_factor_train": 1.12,
    "profit_factor_quarantine": 2.02,
    "n_trades_train": 22,
    "n_trades_quarantine": 4,
    "period": "30j train + 15j quarantaine",
    "updated": "2026-08-05",
}

COMPETITOR_USERNAME = "ChatGPT Trader"
MIN_RISK_REWARD = 1.5   # non négociable sans revalidation

# --- Backtest / broker ---
COMMISSION_PER_LOT_USD = 7.0   # XM standard sur XAUUSD (aller-retour)
SLIPPAGE_TICKS = 1              # 1 tick = 0.10 $ sur XAUUSD
XAUUSD_TICK_SIZE = 0.01
XAUUSD_CONTRACT_SIZE = 100      # 100 oz par lot standard

# --- Live / paper ---
LIVE_ENABLED = os.environ.get("LIVE_ENABLED", "false").lower() == "true"
METAAPI_TOKEN = os.environ.get("METAAPI_TOKEN", "")
METAAPI_ACCOUNT_ID = os.environ.get("METAAPI_ACCOUNT_ID", "")
METAAPI_REGION = os.environ.get("METAAPI_REGION", "new-york")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# --- Dukascopy ---
DUKASCOPY_BASE_URL = "https://datafeed.dukascopy.com/datafeed"
DUKASCOPY_MAX_CONCURRENCY = 2    # Dukascopy est agressif sur le rate limit
DUKASCOPY_RETRY_MAX = 5
DUKASCOPY_TIMEOUT_S = 30
DUKASCOPY_BATCH_SLEEP_S = 1.0    # pause entre batches
DUKASCOPY_RATE_LIMIT_SLEEP_S = 45  # attente longue si 429
# Pour XAUUSD, Dukascopy encode les prix en int × 1000 (or coté avec 3 décimales)
DUKASCOPY_PRICE_DIVISOR_XAUUSD = 1000.0


def summary() -> str:
    return (
        f"Symbol         : {SYMBOL}\n"
        f"History range  : {HISTORY_START} → {HISTORY_END}\n"
        f"Quarantine     : {QUARANTINE_START} → {HISTORY_END}\n"
        f"Timeframes     : {', '.join(TIMEFRAMES)}\n"
        f"Sessions       : {sorted(TRADING_SESSIONS)}\n"
        f"Daily loss max : {DAILY_LOSS_MAX_DOLLARS} $\n"
        f"Max positions  : {MAX_OPEN_POSITIONS}\n"
        f"Live enabled   : {LIVE_ENABLED}\n"
    )


if __name__ == "__main__":
    print(summary())
