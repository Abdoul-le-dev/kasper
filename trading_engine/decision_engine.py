"""
decision_engine.py

Assemble le dossier d'analyse COMPLET (payload maximal) envoyé à Claude:
- Bougies brutes multi-timeframes
- Tous les indicateurs calculés (SCD, IRV, EMA, ATR, RSI, Bollinger)
- Zones support/résistance
- Fibonacci du dernier swing
- VWAP de la session
- Contexte de session (Asie/Londres/NY/overlap)
- DXY (Dollar Index) actuel + variations
- Événements macro à venir (24h)
- Historique des 10 dernières décisions Claude
- État du compte + budget de risque restant

Puis fait valider la décision de Claude par le risk manager (garde-fous durs
conservés).
"""

import logging
from typing import Dict, List, Optional, Any

from . import indicators as ind
from . import zones as zn
from . import price_action as pa
from . import risk_manager as rm
from . import claude_advisor
from . import session_context
from . import dxy_provider
from . import macro_calendar
from . import advanced_indicators as adv
from . import history_provider

logger = logging.getLogger("trading_engine.decision_engine")


# --- Calculs de contexte ---

def compute_bias(payload: Dict) -> Dict:
    h4 = payload["H4"]
    ohlc_h4 = h4["ohlc"]
    ema50_h4 = h4.get("ema50") or ind.ema_last([c["close"] for c in ohlc_h4], 50)
    ema200_h4 = h4.get("ema200") or ind.ema_last([c["close"] for c in ohlc_h4], 200)
    structure_h4 = ind.detect_market_structure(ohlc_h4)
    price = payload["prix"]["actuel"]
    return {"ema50_h4": ema50_h4, "ema200_h4": ema200_h4, "structure_h4": structure_h4, "price": price}


def compute_volatility_regime(payload: Dict) -> Dict:
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
    """Calcule les zones à partir de D1+H4, ou H4 seul si D1 absent (version août)."""
    d1_ohlc = payload.get("D1", {}).get("ohlc") if payload.get("D1") else None
    h4_ohlc = payload["H4"]["ohlc"]
    if d1_ohlc:
        return zn.identify_zones(d1_ohlc, h4_ohlc, max_zones=6)
    # Sans D1 : on utilise H4 deux fois (comme approximation)
    return zn.identify_zones(h4_ohlc, h4_ohlc, max_zones=6)


# --- Utilitaires pour alléger les bougies avant envoi ---

def _round_ohlc(ohlc: List[Dict], decimals: int = 3, max_candles: Optional[int] = None) -> List[Dict]:
    """Arrondit les OHLC pour réduire le poids en tokens, garde les N dernières bougies."""
    if max_candles:
        ohlc = ohlc[-max_candles:]
    return [
        {
            "o": round(c["open"], decimals),
            "h": round(c["high"], decimals),
            "l": round(c["low"], decimals),
            "c": round(c["close"], decimals),
        }
        for c in ohlc
    ]


# --- Assemblage du dossier maximal ---

def build_dossier(payload: Dict) -> Dict[str, Any]:
    """
    Construit le dossier d'analyse ENRICHI envoyé à Claude.
    Volume cible: ~10-15k tokens (bougies brutes + tous les contextes).
    """
    bias = compute_bias(payload)
    volatility = compute_volatility_regime(payload)
    zones = compute_zones(payload)

    price = payload["prix"]["actuel"]
    zone_proximity = zn.zone_proximity_type(price, zones, volatility["atr_current"], max_distance_factor=0.3)
    scd = ind.scd_score(price, bias["ema50_h4"], bias["ema200_h4"], bias["structure_h4"], zone_proximity)

    price_action_buy = pa.detect_price_action_signal(payload["M15"]["ohlc"], "BUY")
    price_action_sell = pa.detect_price_action_signal(payload["M15"]["ohlc"], "SELL")

    # Fibonacci sur H1 (dernier swing)
    fib = adv.fibonacci_levels(payload["H1"]["ohlc"], lookback=50)

    # VWAP session sur M15
    vwap_info = adv.vwap_session(payload["M15"]["ohlc"], session_length_candles=32)

    # Session actuelle
    session = session_context.get_session_info()

    # DXY (peut échouer, on capture proprement)
    try:
        dxy = dxy_provider.get_dxy_context()
    except Exception as exc:
        logger.warning("DXY error: %s", exc)
        dxy = {"available": False, "reason": str(exc)}

    # Macro à venir
    try:
        macro_events = macro_calendar.get_upcoming_events(hours_ahead=24)
    except Exception as exc:
        logger.warning("Macro calendar error: %s", exc)
        macro_events = []

    # Historique des 10 dernières décisions
    try:
        history = history_provider.get_recent_decisions_summary(n=10)
        history_counts = history_provider.count_recent_decisions_by_type(n=10)
    except Exception as exc:
        logger.warning("History provider error: %s", exc)
        history = []
        history_counts = {}

    # Zones avec distance ATR
    zones_with_distance = [
        {
            "prix": round(z["price"], 3),
            "type": z["type"],
            "touches": z["touches"],
            "distance_atr": round(abs(z["price"] - price) / volatility["atr_current"], 2),
        }
        for z in zones
    ]

    # Compte
    compte = payload["compte"]
    perte_du_jour = payload.get("perte_du_jour_cumulee", 0.0)
    trades_perdants = payload.get("nombre_trades_perdants_jour", 0)

    # RSI H1 (calculé si non fourni)
    h1_ohlc = payload["H1"]["ohlc"]
    try:
        rsi_h1 = ind.rsi([c["close"] for c in h1_ohlc], period=14)
    except Exception:
        rsi_h1 = None

    dossier = {
        "moment": payload.get("timestamp"),
        "session": session,
        "prix": {
            "actuel": round(price, 3),
            "bid": payload["prix"].get("bid"),
            "ask": payload["prix"].get("ask"),
            "spread": payload["prix"].get("spread"),
        },
        "bougies_brutes": {
            "H4": _round_ohlc(payload["H4"]["ohlc"], max_candles=100),
            "H1": _round_ohlc(payload["H1"]["ohlc"], max_candles=100),
            "M30": _round_ohlc((payload.get("M30") or {}).get("ohlc", []), max_candles=80),
            "M15": _round_ohlc(payload["M15"]["ohlc"], max_candles=60),
            "M5": _round_ohlc(payload["M5"]["ohlc"], max_candles=60),
        },
        "indicateurs_calcules": {
            "ema50_h4": round(bias["ema50_h4"], 3),
            "ema200_h4": round(bias["ema200_h4"], 3),
            "structure_h4": bias["structure_h4"],
            "prix_vs_ema50_h4": "au-dessus" if price > bias["ema50_h4"] else "en-dessous",
            "prix_vs_ema200_h4": "au-dessus" if price > bias["ema200_h4"] else "en-dessous",
            "scd": scd,
            "atr_h1_courant": round(volatility["atr_current"], 3),
            "atr_h1_moyenne_20": round(volatility["atr_avg20"], 3),
            "irv": round(volatility["irv"], 3),
            "regime_volatilite": volatility["regime"],
            "rsi_h1": round(rsi_h1, 2) if rsi_h1 is not None else None,
        },
        "zones_interet": zones_with_distance,
        "zone_proximite_actuelle": zone_proximity,
        "fibonacci": fib,
        "vwap_session_m15": vwap_info,
        "action_prix_m15": {
            "signal_haussier_present": price_action_buy,
            "signal_baissier_present": price_action_sell,
        },
        "dxy": dxy,
        "macro": {
            "evenements_a_venir_24h": macro_events,
            "evenement_high_impact_dans_60min": any(
                e.get("impact") == "high" and e.get("minutes_avant", 9999) <= 60
                for e in macro_events
            ),
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
        "historique_decisions_recentes": {
            "dernieres_10_decisions": history,
            "compte_par_type": history_counts,
        },
    }
    return dossier


# --- Garde-fous du risk manager ---

def _forced_hold(reason: str, claude_decision: Dict[str, Any]) -> Dict[str, Any]:
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
    decision_type = claude_decision["decision"]
    price = payload["prix"]["actuel"]

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
            "pourcentage_reduction": 50,
            "raisonnement": claude_decision["raisonnement"],
            "confiance": claude_decision["confiance"],
            "risques_identifies": claude_decision.get("risques_identifies", []),
        }

    if decision_type == "ENTER":
        direction = claude_decision.get("direction")
        sl = claude_decision.get("sl_propose")
        tp = claude_decision.get("tp_propose")

        if direction not in ("BUY", "SELL") or sl is None or tp is None:
            return _forced_hold("ENTER incomplet (direction/sl/tp manquants)", claude_decision)

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

    return _forced_hold(f"type de décision inconnu: {decision_type}", claude_decision)


def analyze(payload: Dict[str, Any], client=None) -> Dict[str, Any]:
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

    final_decision["scd"] = dossier["indicateurs_calcules"]["scd"]
    final_decision["irv"] = dossier["indicateurs_calcules"]["irv"]
    final_decision["fqe_score"] = None
    final_decision["session"] = dossier["session"]["session_label"]

    return final_decision
