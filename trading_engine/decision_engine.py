"""
decision_engine.py

Architecture B pure — Claude est le décideur, ce module l'assiste.

Flux à chaque appel:
1. Calculer tous les indicateurs (SCD, IRV, FQE, zones, action de prix, etc.)
2. Assembler un "dossier d'analyse" structuré et lisible par Claude
3. Envoyer le dossier à Claude via claude_advisor.ask_claude
4. Faire valider la décision de Claude par le risk manager (garde-fous durs)
5. Retourner la décision finale au format attendu par l'API

Le risk manager est AU-DESSUS de Claude — il peut refuser ou modifier sa
décision. Claude propose, le code Python dispose.
"""

import logging
from typing import Dict, List, Optional, Any

from . import indicators as ind
from . import zones as zn
from . import price_action as pa
from . import risk_manager as rm
from . import claude_advisor

logger = logging.getLogger("trading_engine.decision_engine")

MACRO_EVENT_BUFFER_MINUTES = 60


# --- Calculs de contexte (inchangés depuis l'ancienne version) ---

def _macro_event_within_buffer(evenements_macro: List[Dict], buffer_minutes: int = MACRO_EVENT_BUFFER_MINUTES) -> bool:
    for event in evenements_macro:
        if event.get("impact") == "high" and event.get("minutes_avant", 9999) <= buffer_minutes:
            return True
    return False


def compute_bias(payload: Dict) -> Dict:
    """Étape A — Biais directionnel (D1/H4)."""
    h4 = payload["H4"]
    ohlc_h4 = h4["ohlc"]
    ema50_h4 = h4.get("ema50") or ind.ema_last([c["close"] for c in ohlc_h4], 50)
    ema200_h4 = h4.get("ema200") or ind.ema_last([c["close"] for c in ohlc_h4], 200)
    structure_h4 = ind.detect_market_structure(ohlc_h4)
    price = payload["prix"]["actuel"]
    return {"ema50_h4": ema50_h4, "ema200_h4": ema200_h4, "structure_h4": structure_h4, "price": price}


def compute_volatility_regime(payload: Dict) -> Dict:
    """Étape B — Régime de volatilité (IRV) basé sur ATR H1."""
    h1 = payload["H1"]
    atr_current = h1.get("atr14")
    atr_avg20 = h1.get("atr14_moy20")
    if atr_current is None or atr_avg20 is None:
        atr_series = ind.atr(h1["ohlc"], period=14)
        atr_current = atr_series[-1]
        atr_avg20 = ind.atr_average(atr_series, lookback=20)
    irv = ind.irv_index(atr_current, atr_avg20)
    regime = ind.irv_regime(irv)
    return {"atr_current": atr_current, "atr_avg20": atr_avg20, "irv": irv, "regime": regime}


def compute_zones(payload: Dict) -> List[Dict]:
    """Étape C — Zones d'intérêt à partir de D1/H4."""
    return zn.identify_zones(payload["D1"]["ohlc"], payload["H4"]["ohlc"], max_zones=4)


# --- Assemblage du dossier d'analyse pour Claude ---

def build_dossier(payload: Dict) -> Dict[str, Any]:
    """
    Construit le dossier d'analyse structuré envoyé à Claude.
    Ne contient QUE les informations calculées et pertinentes — pas les
    centaines de bougies brutes qui gaspilleraient des tokens.
    """
    bias = compute_bias(payload)
    volatility = compute_volatility_regime(payload)
    zones = compute_zones(payload)

    price = payload["prix"]["actuel"]
    zone_proximity = zn.zone_proximity_type(price, zones, volatility["atr_current"], max_distance_factor=0.3)
    scd = ind.scd_score(price, bias["ema50_h4"], bias["ema200_h4"], bias["structure_h4"], zone_proximity)

    price_action_buy = pa.detect_price_action_signal(payload["M15"]["ohlc"], "BUY")
    price_action_sell = pa.detect_price_action_signal(payload["M15"]["ohlc"], "SELL")

    macro_event_soon = _macro_event_within_buffer(payload.get("evenements_macro_a_venir", []))

    zones_with_distance = []
    for z in zones:
        zones_with_distance.append({
            "prix": round(z["price"], 2),
            "type": z["type"],
            "touches": z["touches"],
            "distance_atr": round(abs(z["price"] - price) / volatility["atr_current"], 2),
        })

    compte = payload["compte"]
    perte_du_jour = payload.get("perte_du_jour_cumulee", 0.0)
    trades_perdants = payload.get("nombre_trades_perdants_jour", 0)

    dossier = {
        "moment": payload.get("timestamp"),
        "prix": {
            "actuel": round(price, 2),
            "bid": payload["prix"].get("bid"),
            "ask": payload["prix"].get("ask"),
            "spread": payload["prix"].get("spread"),
        },
        "biais_directionnel": {
            "structure_h4": bias["structure_h4"],
            "prix_vs_ema50_h4": "au-dessus" if price > bias["ema50_h4"] else "en-dessous",
            "prix_vs_ema200_h4": "au-dessus" if price > bias["ema200_h4"] else "en-dessous",
            "ema50_h4": round(bias["ema50_h4"], 2),
            "ema200_h4": round(bias["ema200_h4"], 2),
            "scd": scd,  # -3 à +3
        },
        "volatilite": {
            "atr_h1_courant": round(volatility["atr_current"], 3),
            "atr_h1_moyenne_20": round(volatility["atr_avg20"], 3),
            "irv": round(volatility["irv"], 3),
            "regime": volatility["regime"],  # "compression" | "normal" | "expansion"
        },
        "zones_interet": zones_with_distance,
        "zone_proximite_actuelle": zone_proximity,  # "demand" | "supply" | None
        "action_prix_m15": {
            "signal_haussier_present": price_action_buy,
            "signal_baissier_present": price_action_sell,
        },
        "macro": {
            "evenement_majeur_imminent": macro_event_soon,
            "evenements_a_venir": payload.get("evenements_macro_a_venir", []),
        },
        "compte": {
            "solde": compte.get("solde"),
            "equite": compte.get("equite"),
            "marge_disponible": compte.get("marge_disponible"),
            "positions_ouvertes": compte.get("positions_ouvertes", []),
        },
        "budget_risque": {
            "perte_du_jour_cumulee": perte_du_jour,
            "perte_max_journaliere_restante": max(rm.DAILY_LOSS_MAX_DOLLARS - perte_du_jour, 0),
            "trades_perdants_du_jour": trades_perdants,
            "trades_perdants_restants": max(rm.MAX_LOSING_TRADES_PER_DAY - trades_perdants, 0),
            "positions_ouvertes_max": rm.MAX_OPEN_POSITIONS,
            "positions_ouvertes_actuelles": len(compte.get("positions_ouvertes", [])),
        },
    }
    return dossier


# --- Garde-fous : validation de la décision de Claude par le risk manager ---

def _forced_hold(reason: str, claude_decision: Dict[str, Any]) -> Dict[str, Any]:
    """Décision forcée en HOLD par le risk manager, avec conservation du contexte Claude."""
    return {
        "decision": "HOLD",
        "direction": None,
        "entry": None,
        "sl": None,
        "tp": None,
        "risque_dollars": None,
        "rr_vise": None,
        "raisonnement": f"[Forcé en HOLD par risk manager: {reason}] — Claude avait proposé: {claude_decision.get('decision')} ({claude_decision.get('raisonnement', '')[:200]})",
        "confiance": claude_decision.get("confiance"),
        "risques_identifies": claude_decision.get("risques_identifies", []),
    }


def apply_risk_guardrails(
    claude_decision: Dict[str, Any],
    payload: Dict[str, Any],
    dossier: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Applique tous les garde-fous du risk manager sur la décision de Claude.
    Retourne soit la décision de Claude (validée et complétée), soit un HOLD forcé.
    """
    decision_type = claude_decision["decision"]
    price = payload["prix"]["actuel"]

    # HOLD, EXIT, REDUCE passent tels quels — le risk manager ne bloque QUE les entrées
    if decision_type in ("HOLD", "EXIT"):
        return {
            "decision": decision_type,
            "direction": None,
            "entry": None,
            "sl": None,
            "tp": None,
            "risque_dollars": None,
            "rr_vise": None,
            "raisonnement": claude_decision["raisonnement"],
            "confiance": claude_decision["confiance"],
            "risques_identifies": claude_decision.get("risques_identifies", []),
        }

    if decision_type == "REDUCE":
        return {
            "decision": "REDUCE",
            "direction": None,
            "entry": None,
            "sl": None,
            "tp": None,
            "risque_dollars": None,
            "rr_vise": None,
            "nouveau_sl": claude_decision.get("sl_propose"),
            "pourcentage_reduction": 50,  # fixé par la spec
            "raisonnement": claude_decision["raisonnement"],
            "confiance": claude_decision["confiance"],
            "risques_identifies": claude_decision.get("risques_identifies", []),
        }

    # ENTER — vérifications strictes
    if decision_type == "ENTER":
        direction = claude_decision.get("direction")
        sl = claude_decision.get("sl_propose")
        tp = claude_decision.get("tp_propose")

        if direction not in ("BUY", "SELL") or sl is None or tp is None:
            return _forced_hold("ENTER incomplet (direction/sl/tp manquants)", claude_decision)

        # Portail de risque complet
        compte = payload["compte"]
        risk_check = rm.full_risk_gate(
            daily_loss_cumulative=payload.get("perte_du_jour_cumulee", 0.0),
            open_positions=compte.get("positions_ouvertes", []),
            new_direction=direction,
            losing_trades_today=payload.get("nombre_trades_perdants_jour", 0),
            entry=price,
            sl=sl,
            tp=tp,
        )
        if not risk_check.allowed:
            return _forced_hold(risk_check.reason, claude_decision)

        # Trouver la zone de référence la plus proche pour tracer l'invalidation
        zones = compute_zones(payload)
        nearest = zn.nearest_zone(price, zones)
        zone_reference_price = nearest["price"] if nearest else None

        risk_dollars = rm.enforce_min_risk(rm.RISK_PER_TRADE_MIN_DOLLARS)
        rr = rm.compute_risk_reward(price, sl, tp, direction)

        return {
            "decision": "ENTER",
            "direction": direction,
            "entry": round(price, 2),
            "sl": round(sl, 2),
            "tp": round(tp, 2),
            "risque_dollars": risk_dollars,
            "rr_vise": rr,
            "zone_reference_price": zone_reference_price,
            "raisonnement": claude_decision["raisonnement"],
            "confiance": claude_decision["confiance"],
            "risques_identifies": claude_decision.get("risques_identifies", []),
        }

    # Type de décision inconnu — sécurité
    return _forced_hold(f"type de décision inconnu: {decision_type}", claude_decision)


# --- Point d'entrée principal ---

def analyze(payload: Dict[str, Any], client=None) -> Dict[str, Any]:
    """
    Point d'entrée principal du moteur.
    1. Construit le dossier d'analyse
    2. Appelle Claude via claude_advisor
    3. Valide la décision de Claude via le risk manager
    4. Retourne la décision finale

    `client` optionnel (httpx.Client) permet l'injection pour les tests.
    """
    try:
        dossier = build_dossier(payload)
    except Exception as exc:
        logger.exception("Erreur lors de la construction du dossier d'analyse")
        return {
            "decision": "HOLD",
            "direction": None,
            "entry": None, "sl": None, "tp": None,
            "risque_dollars": None, "rr_vise": None,
            "raisonnement": f"[Erreur technique: impossible de construire le dossier — {exc}]",
            "confiance": "basse",
            "risques_identifies": [f"Erreur build_dossier: {type(exc).__name__}"],
        }

    claude_decision = claude_advisor.ask_claude(dossier, client=client)
    final_decision = apply_risk_guardrails(claude_decision, payload, dossier)

    # Ajouter les métadonnées de contexte pour le journal (mais ne pas polluer l'API)
    final_decision["scd"] = dossier["biais_directionnel"]["scd"]
    final_decision["irv"] = dossier["volatilite"]["irv"]
    final_decision["fqe_score"] = None  # calculé plus haut par Claude si utilisé

    return final_decision
