"""
shared.py

Fonctions partagées entre trade.py, daily_report.py, weekly_report.py,
canal_listener.py et paper_runner.py.

Centralise :
- Envoi Telegram (avec dédup et anti-boucle)
- Journal JSONL unifié
- Kill switch quotidien (calculé depuis MetaApi)
- Helpers de formatage
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone, date
from pathlib import Path
from typing import Optional

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402
from src.metaapi_connector import get_account_summary

logger = logging.getLogger("shared")

# Journal unifié : toutes les positions (scalp + directes) atterrissent ici.
JOURNAL_PATH = config.LOGS_DIR / "trades.jsonl"
BOT_MESSAGES_PATH = config.LOGS_DIR / "bot_own_messages.jsonl"  # anti-boucle canal_listener
EQUITY_SNAPSHOT_PATH = config.LOGS_DIR / "equity_snapshot.json"


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def telegram_send(message: str, tag_own_message: bool = True) -> Optional[int]:
    """Envoie un message Telegram, retourne le message_id si succès.

    Si tag_own_message=True, on stocke l'ID dans bot_own_messages.jsonl
    pour que canal_listener ignore ce message (anti-boucle).
    """
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        logger.warning("Telegram non configuré, skip")
        return None
    try:
        url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
        with httpx.Client(timeout=10) as c:
            r = c.post(url, json={
                "chat_id": config.TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "Markdown",
            })
            r.raise_for_status()
            data = r.json()
            msg_id = data.get("result", {}).get("message_id")
            if tag_own_message and msg_id:
                with BOT_MESSAGES_PATH.open("a") as f:
                    f.write(json.dumps({
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "message_id": msg_id,
                    }) + "\n")
            return msg_id
    except Exception as e:
        logger.warning("Telegram send failed: %s", e)
        return None


def load_bot_message_ids() -> set[int]:
    """Charge tous les IDs de messages postés par notre bot (anti-boucle)."""
    if not BOT_MESSAGES_PATH.exists():
        return set()
    ids = set()
    with BOT_MESSAGES_PATH.open() as f:
        for line in f:
            try:
                ids.add(json.loads(line)["message_id"])
            except Exception:
                pass
    return ids


# ---------------------------------------------------------------------------
# Journal unifié
# ---------------------------------------------------------------------------

def journal_append(entry: dict) -> None:
    """Append au journal unifié. `source` distingue scalp vs direct (usage interne uniquement).
    """
    entry = {
        "ts_utc": datetime.now(timezone.utc).isoformat(),
        **entry,
    }
    with JOURNAL_PATH.open("a") as f:
        f.write(json.dumps(entry, default=str) + "\n")


def journal_read(after: Optional[datetime] = None) -> list[dict]:
    """Lit le journal, filtre après un timestamp UTC si fourni."""
    if not JOURNAL_PATH.exists():
        return []
    entries = []
    with JOURNAL_PATH.open() as f:
        for line in f:
            try:
                e = json.loads(line)
                if after is None:
                    entries.append(e)
                    continue
                ts = datetime.fromisoformat(e["ts_utc"].replace("Z", "+00:00"))
                if ts >= after:
                    entries.append(e)
            except Exception:
                pass
    return entries


# ---------------------------------------------------------------------------
# Équité & kill switch (partagé entre scalp et direct)
# ---------------------------------------------------------------------------

def get_equity_start_of_day() -> Optional[float]:
    """Retourne l'équité de début du jour UTC courant. Persisté sur disque.

    Utilisé par tous les composants pour partager le même point de référence.
    """
    today = datetime.now(timezone.utc).date().isoformat()
    if EQUITY_SNAPSHOT_PATH.exists():
        try:
            snap = json.loads(EQUITY_SNAPSHOT_PATH.read_text())
            if snap.get("date") == today:
                return snap.get("equity_start")
        except Exception:
            pass
    # Nouveau jour : on capture l'équité actuelle
    try:
        summary = get_account_summary()
        eq = summary["equite"]
        EQUITY_SNAPSHOT_PATH.write_text(json.dumps({
            "date": today,
            "equity_start": eq,
            "captured_at": datetime.now(timezone.utc).isoformat(),
        }))
        return eq
    except Exception as e:
        logger.error("Cannot capture equity: %s", e)
        return None


def get_daily_loss() -> float:
    """Perte cumulée du jour en $ (positive si perte)."""
    eq_start = get_equity_start_of_day()
    if eq_start is None:
        return 0.0
    try:
        summary = get_account_summary()
        return -(summary["equite"] - eq_start)
    except Exception:
        return 0.0


def is_kill_switch_active() -> bool:
    """True si perte cumulée >= seuil."""
    return get_daily_loss() >= config.DAILY_LOSS_MAX_DOLLARS


def kill_switch_message() -> str:
    """Message envoyé sur Telegram quand un signal est détecté mais budget atteint.

    Ton : algorithme observant. Aucune mention de 'perte' explicite, on parle
    de 'budget de risque du jour' pour rester neutre.
    """
    return (
        "🟠 *Configuration favorable détectée*\n\n"
        "Le budget de risque quotidien est atteint. "
        "Aucune nouvelle position ne sera prise jusqu'à la prochaine séance."
    )
