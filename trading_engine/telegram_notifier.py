"""
telegram_notifier.py

Envoie les décisions du moteur (ENTER / EXIT / REDUCE / HOLD) et les alertes
critiques vers un canal Telegram, pour un suivi en temps réel.

Configuration (variables d'environnement) :
    TELEGRAM_BOT_TOKEN   : token du bot (obtenu via @BotFather)
    TELEGRAM_CHAT_ID     : identifiant du chat/canal/groupe destinataire
    TELEGRAM_NOTIFY_HOLD : "true"/"false" — envoyer aussi les HOLD (défaut: false,
                           pour ne pas noyer le canal sous les cycles de 30 min sans action)

Ce module est volontairement séparé en deux couches :
1. Formatage (fonctions pures, testables sans réseau) : format_*_message()
2. Envoi (appel HTTP réel vers l'API Telegram) : send_message(), notify_decision()

La couche envoi ne doit JAMAIS faire planter le moteur de décision : toute erreur
réseau ou de configuration est journalisée mais absorbée (le trading continue
même si Telegram est indisponible).
"""

import os
import logging
from typing import Dict, Optional

import httpx

logger = logging.getLogger("trading_engine.telegram_notifier")

TELEGRAM_API_BASE = "https://api.telegram.org"


class TelegramConfigError(Exception):
    """Levée quand la configuration Telegram est absente ou invalide."""
    pass


def get_config() -> Dict[str, str]:
    """Lit la configuration depuis les variables d'environnement."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise TelegramConfigError(
            "TELEGRAM_BOT_TOKEN et TELEGRAM_CHAT_ID doivent être définis en variables d'environnement"
        )
    return {"token": token, "chat_id": chat_id}


def notify_hold_enabled() -> bool:
    return os.environ.get("TELEGRAM_NOTIFY_HOLD", "false").lower() == "true"


# --- Formatage des messages (fonctions pures, testables sans réseau) ---

def _fmt(value: Optional[float]) -> str:
    return "-" if value is None else f"{value:.2f}"


def format_enter_message(decision: Dict) -> str:
    return (
        f"🟢 *ENTRÉE {decision.get('direction')}*\n"
        f"Prix d'entrée : `{_fmt(decision.get('entry'))}`\n"
        f"SL : `{_fmt(decision.get('sl'))}`  |  TP : `{_fmt(decision.get('tp'))}`\n"
        f"Risque : `{_fmt(decision.get('risque_dollars'))}$`  |  R:R visé : `{_fmt(decision.get('rr_vise'))}`\n"
        f"SCD : `{decision.get('scd')}`  |  IRV : `{_fmt(decision.get('irv'))}`  |  FQE : `{decision.get('fqe_score')}/5`\n"
        f"_Raisonnement : {decision.get('raisonnement', '')}_"
    )


def format_exit_message(decision: Dict) -> str:
    return (
        f"🔴 *SORTIE DE POSITION*\n"
        f"_Raison : {decision.get('raisonnement', '')}_"
    )


def format_reduce_message(decision: Dict) -> str:
    return (
        f"🟡 *RÉDUCTION PARTIELLE — 1R atteint*\n"
        f"Nouveau SL (breakeven) : `{_fmt(decision.get('nouveau_sl'))}`\n"
        f"Pourcentage réduit : `{decision.get('pourcentage_reduction')}%`\n"
        f"_{decision.get('raisonnement', '')}_"
    )


def format_hold_message(decision: Dict) -> str:
    return (
        f"⚪ *HOLD*\n"
        f"SCD : `{decision.get('scd')}`  |  IRV : `{_fmt(decision.get('irv'))}`  "
        f"|  FQE : `{decision.get('fqe_score')}/5`\n"
        f"_{decision.get('raisonnement', '')}_"
    )


def format_urgent_alert_message(reason: str) -> str:
    return f"🚨 *INTERVENTION IMMÉDIATE REQUISE*\n_{reason}_"


def format_daily_summary_message(summary: Dict) -> str:
    """
    summary attendu: {
        "date": "2026-07-30",
        "nb_trades": 3, "nb_gagnants": 2, "nb_perdants": 1,
        "pnl_du_jour": 7.5, "solde_actuel": 107.5
    }
    """
    return (
        f"📊 *RÉSUMÉ DU JOUR — {summary.get('date')}*\n"
        f"Trades : `{summary.get('nb_trades')}`  "
        f"(✅ `{summary.get('nb_gagnants')}` / ❌ `{summary.get('nb_perdants')}`)\n"
        f"P&L du jour : `{_fmt(summary.get('pnl_du_jour'))}$`\n"
        f"Solde actuel : `{_fmt(summary.get('solde_actuel'))}$`"
    )


DECISION_FORMATTERS = {
    "ENTER": format_enter_message,
    "EXIT": format_exit_message,
    "REDUCE": format_reduce_message,
    "HOLD": format_hold_message,
}


def format_decision_message(decision: Dict) -> str:
    """Sélectionne le bon formateur selon le type de décision."""
    formatter = DECISION_FORMATTERS.get(decision.get("decision"))
    if formatter is None:
        raise ValueError(f"Type de décision inconnu: {decision.get('decision')}")
    return formatter(decision)


# --- Envoi (couche réseau) ---

def send_message(text: str, config: Optional[Dict[str, str]] = None, client: Optional[httpx.Client] = None) -> bool:
    """
    Envoie un message texte au chat Telegram configuré.
    Retourne True si l'envoi a réussi, False sinon (jamais d'exception propagée
    vers l'appelant, pour ne jamais bloquer le moteur de décision).

    `client` : permet d'injecter un client httpx (utilisé pour les tests, avec
    un transport mocké) — sinon un client réel est créé.
    """
    try:
        cfg = config or get_config()
    except TelegramConfigError as exc:
        logger.warning("Notification Telegram ignorée: %s", exc)
        return False

    url = f"{TELEGRAM_API_BASE}/bot{cfg['token']}/sendMessage"
    payload = {
        "chat_id": cfg["chat_id"],
        "text": text,
        "parse_mode": "Markdown",
    }

    owns_client = client is None
    http_client = client or httpx.Client(timeout=10.0)
    try:
        response = http_client.post(url, json=payload)
        if response.status_code != 200:
            logger.error("Échec envoi Telegram (%s): %s", response.status_code, response.text)
            return False
        return True
    except httpx.HTTPError as exc:
        logger.error("Erreur réseau lors de l'envoi Telegram: %s", exc)
        return False
    finally:
        if owns_client:
            http_client.close()


def notify_decision(decision: Dict, config: Optional[Dict[str, str]] = None, client: Optional[httpx.Client] = None) -> bool:
    """
    Point d'entrée principal : notifie une décision du moteur si elle est
    pertinente (ENTER/EXIT/REDUCE toujours, HOLD seulement si activé).
    """
    decision_type = decision.get("decision")
    if decision_type == "HOLD" and not notify_hold_enabled():
        return False

    try:
        text = format_decision_message(decision)
    except ValueError as exc:
        logger.error("Impossible de formater la décision pour Telegram: %s", exc)
        return False

    return send_message(text, config=config, client=client)


def notify_urgent_alert(reason: str, config: Optional[Dict[str, str]] = None, client: Optional[httpx.Client] = None) -> bool:
    """Notifie une intervention immédiate (section 11 de la spec)."""
    text = format_urgent_alert_message(reason)
    return send_message(text, config=config, client=client)


def notify_daily_summary(summary: Dict, config: Optional[Dict[str, str]] = None, client: Optional[httpx.Client] = None) -> bool:
    """Notifie le résumé journalier."""
    text = format_daily_summary_message(summary)
    return send_message(text, config=config, client=client)