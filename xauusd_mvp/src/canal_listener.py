"""
canal_listener.py

Écoute passive du canal Telegram. Réagit uniquement aux messages postés par
"ChatGPT Trader". Ignore ses propres messages (anti-boucle) et ceux de tout autre
utilisateur.

Ton des réponses : sarcastique + analytique. Félicite si mérité, critique si mérité.
Compare avec le prix réel du marché quand ChatGPT poste des chiffres.

IMPORTANT : ce script utilise le mode long polling du bot Telegram.
Prérequis : le bot doit être admin du groupe OU son "privacy mode" doit être
désactivé via @BotFather → /setprivacy → Disable.
"""

from __future__ import annotations

import logging
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402
from src.metaapi_connector import get_pricing
from src.shared import telegram_send, load_bot_message_ids

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(config.LOGS_DIR / "canal_listener.log"),
    ],
)
logger = logging.getLogger("canal_listener")


POLL_TIMEOUT = 30  # long polling
STATE_PATH = config.LOGS_DIR / "canal_listener_offset.txt"


def get_last_offset() -> int:
    if STATE_PATH.exists():
        try:
            return int(STATE_PATH.read_text().strip() or "0")
        except Exception:
            return 0
    return 0


def save_offset(offset: int) -> None:
    STATE_PATH.write_text(str(offset))


def is_from_competitor(msg: dict) -> bool:
    """True si le message vient de 'ChatGPT Trader'.

    On check plusieurs champs pour être robuste :
      - from.first_name / last_name
      - from.username
    """
    frm = msg.get("from", {})
    name = f"{frm.get('first_name','')} {frm.get('last_name','')}".strip()
    uname = frm.get("username", "") or ""
    target = config.COMPETITOR_USERNAME.lower()
    return (target in name.lower()) or (target.replace(" ", "").lower() in uname.lower())


def extract_numbers(text: str) -> list[float]:
    """Extrait tous les nombres décimaux du texte (utile pour comparer aux prix marché)."""
    # match des flottants type 4245.50 ou 4245 ou -12.30
    pattern = r"-?\d+(?:\.\d+)?"
    return [float(x) for x in re.findall(pattern, text)]


def looks_like_report(text: str) -> bool:
    """Heuristique : est-ce que ce message ressemble à un rapport de perf ?"""
    keywords = ["p&l", "pnl", "profit", "loss", "win rate", "trades", "%",
                "bilan", "rapport", "équité", "equity"]
    tl = text.lower()
    return any(k in tl for k in keywords)


def looks_like_position(text: str) -> bool:
    """Heuristique : ressemble à une prise de position ?"""
    tl = text.lower()
    return ("buy" in tl or "sell" in tl or "long" in tl or "short" in tl) and \
           ("entry" in tl or "sl" in tl or "tp" in tl or "@" in tl)


def compose_reaction_to_position(text: str) -> Optional[str]:
    """Réaction à une prise de position de ChatGPT."""
    nums = extract_numbers(text)
    # On tente de deviner le prix d'entrée (nombre le plus élevé qui ressemble à un prix or)
    price_candidates = [n for n in nums if 3500 <= n <= 5000]
    if not price_candidates:
        return None

    entry_guess = price_candidates[0]

    try:
        market = get_pricing()
        mid = (market["bid"] + market["ask"]) / 2
        delta = abs(entry_guess - mid)
    except Exception as e:
        logger.warning("Can't fetch market to compare: %s", e)
        return f"🎭 Encore un pari, GPT. On verra bien où ça mène."

    # Ton sarcastique + analytique
    tl = text.lower()
    is_buy = "buy" in tl or "long" in tl
    is_sell = "sell" in tl or "short" in tl

    reactions = []
    if delta > 15:
        reactions.append(
            f"🤨 Entry annoncée à `{entry_guess:.2f}`, marché à `{mid:.2f}` "
            f"(écart {delta:.2f} $). Tu tradais quel actif exactement ?"
        )
    elif is_buy and mid < entry_guess - 3:
        reactions.append(
            f"📉 Long à `{entry_guess:.2f}`, on est déjà à `{mid:.2f}`. "
            f"C'est ce qu'on appelle un timing... perfectible."
        )
    elif is_sell and mid > entry_guess - 3:
        reactions.append(
            f"📈 Short pris à `{entry_guess:.2f}`, spot à `{mid:.2f}`. "
            f"Le marché a l'air d'un autre avis pour l'instant."
        )
    else:
        reactions.append(
            f"👀 Noté. Entry `{entry_guess:.2f}` vs marché `{mid:.2f}`. "
            f"On regarde qui a raison sur ce coup."
        )

    return "\n".join(reactions)


def compose_reaction_to_report(text: str) -> Optional[str]:
    """Réaction à un rapport de performance de ChatGPT."""
    nums = extract_numbers(text)
    tl = text.lower()

    # Détecter perte ou gain
    has_negative = any(n < 0 for n in nums) or "loss" in tl or "-$" in tl or "perte" in tl
    pct_candidates = [n for n in nums if -100 < n < 100 and "." in f"{n}"]
    has_high_gain = any(n > 3 for n in pct_candidates)
    has_high_loss = any(n < -3 for n in pct_candidates)

    if has_high_gain and not has_high_loss:
        return (
            "👏 Pas mal, GPT. La chance sourit aux audacieux — reste à voir "
            "combien de séances tu tiens cette cadence avant le retour à la moyenne."
        )
    if has_high_loss:
        return (
            "😬 Aïe. La journée n'a pas été tendre. "
            "C'est ce qui arrive quand on prend le marché pour un modèle de langage."
        )
    if has_negative:
        return (
            "🤷 Journée dans le rouge. Ça arrive à tout le monde. "
            "Enfin, presque."
        )
    return (
        "📊 Rapport noté. Discret sur les détails, comme d'habitude. "
        "On garde en tête pour le bilan hebdo."
    )


def handle_message(msg: dict) -> None:
    """Traite un message reçu."""
    text = msg.get("text", "") or ""
    if not text.strip():
        return

    # Anti-boucle : ignorer nos propres messages
    if msg.get("message_id") in load_bot_message_ids():
        return

    # Filtrer sur ChatGPT Trader
    if not is_from_competitor(msg):
        logger.debug("Message ignoré (pas de ChatGPT Trader): %s", text[:50])
        return

    logger.info("Message ChatGPT reçu: %s", text[:200])

    # Décider du type de réaction
    reaction = None
    if looks_like_position(text):
        reaction = compose_reaction_to_position(text)
    elif looks_like_report(text):
        reaction = compose_reaction_to_report(text)

    if reaction:
        telegram_send(reaction)
        logger.info("Réaction postée")
    else:
        logger.info("Aucune réaction pertinente identifiée")


def poll_loop() -> None:
    logger.info("Starting canal_listener (compétiteur: '%s')", config.COMPETITOR_USERNAME)
    offset = get_last_offset()
    base_url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}"

    while True:
        try:
            with httpx.Client(timeout=POLL_TIMEOUT + 10) as client:
                r = client.get(
                    f"{base_url}/getUpdates",
                    params={"offset": offset + 1, "timeout": POLL_TIMEOUT},
                )
                r.raise_for_status()
                data = r.json()

            if not data.get("ok"):
                logger.warning("Telegram getUpdates not OK: %s", data)
                time.sleep(5)
                continue

            for update in data.get("result", []):
                update_id = update.get("update_id", 0)
                offset = max(offset, update_id)
                msg = update.get("message") or update.get("channel_post")
                if msg:
                    try:
                        handle_message(msg)
                    except Exception as e:
                        logger.error("handle_message error: %s", e, exc_info=True)

            save_offset(offset)

        except httpx.TimeoutException:
            continue  # normal en long polling
        except Exception as e:
            logger.error("Poll error: %s", e)
            time.sleep(10)


def main() -> None:
    if not config.TELEGRAM_BOT_TOKEN:
        print("❌ TELEGRAM_BOT_TOKEN manquant dans .env")
        sys.exit(1)
    poll_loop()


if __name__ == "__main__":
    main()
