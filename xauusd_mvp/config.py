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
    pass  # dotenv optionnel

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
DUKASCOPY_INSTRUMENT = "XAUUSD"
BROKER_SYMBOL = os.environ.get("METAAPI_SYMBOL", "XAUUSD")

# --- Fenêtre historique ---
# MVP allégé : 45 jours au total, dont 15 jours en quarantaine.
# → 30 jours train / 15 jours quarantaine
TODAY = date.today()
HISTORY_END = TODAY - timedelta(days=1)
HISTORY_START = HISTORY_END - timedelta(days=45)
QUARANTINE_START = HISTORY_END - timedelta(days=15)

# --- Timeframes ---
TIMEFRAMES = ["M1", "M5", "M15", "M30", "H1"]
TIMEFRAME_MINUTES = {"M1": 1, "M5": 5, "M15": 15, "M30": 30, "H1": 60}

# --- Sessions (UTC, sans DST) ---
LONDON_START_HOUR = 7
LONDON_END_HOUR = 16
NY_START_HOUR = 13
NY_END_HOUR = 22

TRADING_SESSIONS = {"london", "new_york", "overlap_london_ny"}

# --- Risque (kill switch dur) ---
DAILY_LOSS_MAX_DOLLARS = float(os.environ.get("DAILY_LOSS_MAX_DOLLARS", "50"))
MAX_OPEN_POSITIONS = 1
MIN_RISK_REWARD = 1.5

# --- Backtest / broker ---
COMMISSION_PER_LOT_USD = 7.0
SLIPPAGE_TICKS = 1
XAUUSD_TICK_SIZE = 0.01
XAUUSD_CONTRACT_SIZE = 100

# --- Live / paper ---
LIVE_ENABLED = os.environ.get("LIVE_ENABLED", "false").lower() == "true"
METAAPI_TOKEN = os.environ.get("METAAPI_TOKEN", "")
METAAPI_ACCOUNT_ID = os.environ.get("METAAPI_ACCOUNT_ID", "")
METAAPI_REGION = os.environ.get("METAAPI_REGION", "new-york")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# --- Dukascopy (rate limit prudent) ---
DUKASCOPY_BASE_URL = "https://datafeed.dukascopy.com/datafeed"
DUKASCOPY_MAX_CONCURRENCY = 2
DUKASCOPY_RETRY_MAX = 5
DUKASCOPY_TIMEOUT_S = 30
DUKASCOPY_BATCH_SLEEP_S = 1.0
DUKASCOPY_RATE_LIMIT_SLEEP_S = 45
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