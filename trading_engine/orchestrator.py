"""
orchestrator.py

Le chef d'orchestre qui tourne sur le serveur : collecte les données OANDA,
appelle le moteur de décision (via l'API HTTP locale déjà testée dans api.py,
ce qui réutilise sans dupliquer la logique de notification Telegram), exécute
les ordres résultants via OANDA, et journalise chaque décision.

Boucle principale :
- run_cycle() : un cycle complet (déclenché toutes les 30 min via schedule,
  ou immédiatement sur alerte pattern détectée par monitor_urgent_conditions()).
- monitor_urgent_conditions() : vérification légère toutes les ~60s (spread
  anormal, gap de prix) — section 11 de la spec, sans attendre le cycle de 30 min.

Lancement :
    python3 -m trading_engine.orchestrator
"""

import os
import logging
import time
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List

import httpx
import schedule

from . import metaapi_connector as broker
from . import journal
from . import telegram_notifier as tg

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("trading_engine.orchestrator")

ENGINE_BASE_URL = os.environ.get("ENGINE_BASE_URL", "http://127.0.0.1:8000")
INSTRUMENT = os.environ.get("METAAPI_SYMBOL", "XAUUSD")

# Historique de spread pour détecter un élargissement anormal (section 11)
_SPREAD_HISTORY: List[float] = []
_SPREAD_HISTORY_MAXLEN = 200
_last_price_seen: Optional[float] = None


def _today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# --- Construction du payload (section 10) ---

def build_payload(declencheur: str = "cyclique_30min", client: Optional[httpx.Client] = None) -> Dict[str, Any]:
    """Assemble le payload complet à partir des données OANDA + état local (journal)."""
    config = broker.get_config()

    account = broker.get_account_summary(config=config, client=client)
    pricing = broker.get_pricing(instrument=INSTRUMENT, config=config, client=client)

    d1 = broker.get_candles("D1", count=250, instrument=INSTRUMENT, config=config, client=client)
    h4 = broker.get_candles("H4", count=250, instrument=INSTRUMENT, config=config, client=client)
    h1 = broker.get_candles("H1", count=60, instrument=INSTRUMENT, config=config, client=client)
    m15 = broker.get_candles("M15", count=30, instrument=INSTRUMENT, config=config, client=client)
    m5 = broker.get_candles("M5", count=30, instrument=INSTRUMENT, config=config, client=client)

    raw_trades = broker.get_open_trades(instrument=INSTRUMENT, config=config, client=client)
    positions_ouvertes = []
    for t in raw_trades:
        meta = journal.get_position_metadata(t["trade_id"]) or {}
        positions_ouvertes.append({
            "trade_id": t["trade_id"],
            "direction": t["direction"],
            "entry": t["entry"],
            "sl": t["sl"],
            "tp": t["tp"],
            "units": t["units"],
            "zone_reference_price": meta.get("zone_reference_price"),
            "partial_exit_taken": meta.get("partial_exit_taken", False),
        })

    daily_state = journal.get_daily_state(_today_str())

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "compte": {
            "solde": account["solde"],
            "equite": account["equite"],
            "marge_utilisee": account["marge_utilisee"],
            "marge_disponible": account["marge_disponible"],
            "positions_ouvertes": positions_ouvertes,
        },
        "prix": {
            "actuel": pricing["actuel"],
            "bid": pricing["bid"],
            "ask": pricing["ask"],
            "spread": pricing["spread"],
        },
        "D1": {"ohlc": d1},
        "H4": {"ohlc": h4},
        "H1": {"ohlc": h1},
        "M15": {"ohlc": m15},
        "M5": {"ohlc": m5},
        # NOTE: aucune source de calendrier économique n'est branchée ici.
        # À connecter (ex: API calendrier macro) si l'on veut activer le
        # critère FQE #5 et l'invalidation macro (sections 5c et 7).
        "evenements_macro_a_venir": [],
        "declencheur_alerte": declencheur,
        "perte_du_jour_cumulee": daily_state["perte_du_jour_cumulee"],
        "nombre_trades_perdants_jour": daily_state["nombre_trades_perdants_jour"],
    }


# --- Appel au moteur de décision (via l'API locale déjà testée) ---

def call_engine(payload: Dict[str, Any], client: Optional[httpx.Client] = None) -> Dict[str, Any]:
    """Appelle POST /analyze sur l'API locale et retourne la décision."""
    owns_client = client is None
    http_client = client or httpx.Client(timeout=30.0)
    try:
        response = http_client.post(f"{ENGINE_BASE_URL}/analyze", json=payload)
        response.raise_for_status()
        return response.json()
    finally:
        if owns_client:
            http_client.close()


# --- Exécution de la décision ---

def execute_decision(decision: Dict[str, Any], payload: Dict[str, Any], client: Optional[httpx.Client] = None) -> None:
    """
    Traduit la décision du moteur en actions réelles côté OANDA, et met à jour
    le journal / l'état journalier en conséquence.
    """
    config = broker.get_config()
    decision_type = decision.get("decision")

    if decision_type == "ENTER":
        direction = decision["direction"]
        entry = decision["entry"]
        sl = decision["sl"]
        tp = decision["tp"]
        risk_dollars = decision["risque_dollars"]

        units = broker.calculate_units(risk_dollars, abs(entry - sl), direction)
        result = broker.place_market_order(direction, abs(units), sl, tp, instrument=INSTRUMENT, config=config, client=client)

        # MetaApi retourne positionId au niveau racine (contrairement à OANDA
        # qui l'imbrique sous orderFillTransaction.tradeOpened.tradeID)
        trade_id = result.get("positionId") or result.get("orderId")

        if trade_id:
            journal.save_position_metadata(str(trade_id), {
                "zone_reference_price": decision.get("zone_reference_price"),
                "partial_exit_taken": False,
            })
            logger.info("Position ouverte: trade_id=%s direction=%s volume=%s", trade_id, direction, units)
        else:
            logger.warning("Ordre envoyé mais aucun positionId retourné — vérifier manuellement: %s", result)

    elif decision_type == "EXIT":
        # get_open_trades inclut désormais le champ 'profit' (P&L flottant courant),
        # ce qui évite un appel supplémentaire après clôture.
        open_trades = broker.get_open_trades(instrument=INSTRUMENT, config=config, client=client)
        for t in open_trades:
            pnl = float(t.get("profit", 0.0))
            broker.close_trade(t["trade_id"], config=config, client=client)
            is_loss = pnl < 0
            journal.update_daily_state(_today_str(), loss_delta=abs(pnl) if is_loss else 0.0, is_losing_trade=is_loss)
            journal.delete_position_metadata(t["trade_id"])
            logger.info("Position fermée: trade_id=%s pnl=%s", t["trade_id"], pnl)

    elif decision_type == "REDUCE":
        open_trades = broker.get_open_trades(instrument=INSTRUMENT, config=config, client=client)
        for t in open_trades:
            # units en lots (float), on ferme le pourcentage demandé avec un minimum de 0.01 lot
            half_volume = round(t["units"] * (decision.get("pourcentage_reduction", 50) / 100), 2)
            half_volume = max(half_volume, 0.01)
            broker.close_trade(t["trade_id"], units=half_volume, config=config, client=client)
            new_sl = decision.get("nouveau_sl")
            if new_sl is not None:
                broker.set_trade_stop_loss(t["trade_id"], new_sl, config=config, client=client)
            meta = journal.get_position_metadata(t["trade_id"]) or {}
            meta["partial_exit_taken"] = True
            journal.save_position_metadata(t["trade_id"], meta)
            logger.info("Réduction partielle appliquée: trade_id=%s volume_ferme=%s", t["trade_id"], half_volume)

    elif decision_type == "HOLD":
        logger.info("HOLD — aucune action")

    else:
        logger.error("Type de décision inconnu, aucune action exécutée: %s", decision_type)


# --- Cycle complet ---

def run_cycle(declencheur: str = "cyclique_30min") -> Dict[str, Any]:
    """Un cycle complet : collecte -> décision -> exécution -> journalisation."""
    try:
        payload = build_payload(declencheur=declencheur)
        decision = call_engine(payload)
        execute_decision(decision, payload)
        journal.log_decision(payload, decision)
        return decision
    except Exception:
        logger.exception("Erreur pendant le cycle d'analyse — cycle abandonné, aucune position modifiée")
        try:
            tg.notify_urgent_alert("Erreur technique pendant un cycle d'analyse — vérification manuelle requise")
        except Exception:
            pass
        raise


# --- Surveillance légère (section 11) ---

def monitor_urgent_conditions() -> None:
    """
    Vérification rapide (spread, gap) sans attendre le cycle de 30 minutes.
    À appeler toutes les ~60 secondes.
    """
    global _last_price_seen
    try:
        config = broker.get_config()
        pricing = broker.get_pricing(instrument=INSTRUMENT, config=config)
    except Exception:
        logger.exception("Impossible de vérifier les conditions urgentes")
        return

    spread = pricing["spread"]
    _SPREAD_HISTORY.append(spread)
    if len(_SPREAD_HISTORY) > _SPREAD_HISTORY_MAXLEN:
        _SPREAD_HISTORY.pop(0)

    if len(_SPREAD_HISTORY) >= 20:
        avg_spread = sum(_SPREAD_HISTORY) / len(_SPREAD_HISTORY)
        if avg_spread > 0 and spread > 3 * avg_spread:
            tg.notify_urgent_alert(f"Spread anormalement élargi: {spread:.2f} (moyenne récente: {avg_spread:.2f})")

    if _last_price_seen is not None:
        gap = abs(pricing["actuel"] - _last_price_seen)
        if avg_spread_gap_threshold(gap):
            tg.notify_urgent_alert(f"Gap de prix significatif détecté: {gap:.2f}")
    _last_price_seen = pricing["actuel"]


def avg_spread_gap_threshold(gap: float, threshold: float = 3.0) -> bool:
    """Seuil de gap jugé significatif (en $ sur XAU_USD) — ajustable selon la volatilité observée."""
    return gap > threshold


# --- Boucle principale ---

def main() -> None:
    logger.info("Démarrage de l'orchestrateur — cycle toutes les 30 min, surveillance toutes les 60s")

    schedule.every(30).minutes.do(run_cycle, declencheur="cyclique_30min")
    schedule.every(60).seconds.do(monitor_urgent_conditions)

    # Premier cycle immédiat au démarrage
    run_cycle(declencheur="cyclique_30min")

    while True:
        schedule.run_pending()
        time.sleep(1)


if __name__ == "__main__":
    main()