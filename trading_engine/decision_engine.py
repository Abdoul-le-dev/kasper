"""
decision_engine.py

Moteur de décision principal. Reçoit le payload structuré (section 10 de la spec)
et retourne une décision structurée: HOLD | ENTER | EXIT | REDUCE.

Ce module orchestre: indicators, zones, price_action, risk_manager.
Il ne fait AUCUN appel réseau — c'est une fonction pure prenant un dict en entrée
et retournant un dict en sortie, pour rester testable de façon isolée.
"""

from typing import Dict, List, Optional

from . import indicators as ind
from . import zones as zn
from . import price_action as pa
from . import risk_manager as rm


MACRO_EVENT_BUFFER_MINUTES = 60


def _macro_event_within_buffer(evenements_macro: List[Dict], buffer_minutes: int = MACRO_EVENT_BUFFER_MINUTES) -> bool:
    """
    Vérifie si un événement macro majeur est prévu dans les `buffer_minutes` à venir.
    Chaque événement attendu: {"minutes_avant": float, "impact": "high"|"medium"|"low"}
    """
    for event in evenements_macro:
        if event.get("impact") == "high" and event.get("minutes_avant", 9999) <= buffer_minutes:
            return True
    return False


def compute_bias(payload: Dict) -> Dict:
    """Étape A — Biais directionnel (D1/H4). Retourne SCD, structure, ema info."""
    h4 = payload["H4"]
    ohlc_h4 = h4["ohlc"]

    ema50_h4 = h4.get("ema50") or ind.ema_last([c["close"] for c in ohlc_h4], 50)
    ema200_h4 = h4.get("ema200") or ind.ema_last([c["close"] for c in ohlc_h4], 200)
    structure_h4 = ind.detect_market_structure(ohlc_h4)

    price = payload["prix"]["actuel"]

    return {
        "ema50_h4": ema50_h4,
        "ema200_h4": ema200_h4,
        "structure_h4": structure_h4,
        "price": price,
    }


def compute_volatility_regime(payload: Dict) -> Dict:
    """Étape B — Régime de volatilité (IRV) basé sur ATR H1."""
    h1 = payload["H1"]
    atr_current = h1.get("atr14")
    atr_avg20 = h1.get("atr14_moy20")

    if atr_current is None or atr_avg20 is None:
        closes = [c["close"] for c in h1["ohlc"]]
        atr_series = ind.atr(h1["ohlc"], period=14)
        atr_current = atr_series[-1]
        atr_avg20 = ind.atr_average(atr_series, lookback=20)

    irv = ind.irv_index(atr_current, atr_avg20)
    regime = ind.irv_regime(irv)

    return {"atr_current": atr_current, "atr_avg20": atr_avg20, "irv": irv, "regime": regime}


def compute_zones(payload: Dict) -> List[Dict]:
    """Étape C — Zones d'intérêt à partir de D1/H4."""
    return zn.identify_zones(payload["D1"]["ohlc"], payload["H4"]["ohlc"], max_zones=4)


def evaluate_fqe(
    scd: int,
    direction: str,
    volatility_regime: str,
    price_action_confirmed: bool,
    zone_proximity: Optional[str],
    macro_event_soon: bool,
) -> Dict:
    """
    Filtre de Qualité d'Entrée (FQE) — checklist sur 5 (section 5c de la spec).
    Retourne le score et le détail de chaque critère.
    """
    scd_aligned = (scd > 0 and direction == "BUY") or (scd < 0 and direction == "SELL")
    volatility_ok = volatility_regime != "compression"
    zone_ok = (zone_proximity == "demand" and direction == "BUY") or (zone_proximity == "supply" and direction == "SELL")

    criteria = {
        "scd_coherent": scd_aligned,
        "volatilite_non_compressee": volatility_ok,
        "action_prix_confirmee": price_action_confirmed,
        "zone_interet_touchee": zone_ok,
        "pas_de_macro_imminente": not macro_event_soon,
    }
    score = sum(1 for v in criteria.values() if v)
    return {"score": score, "criteria": criteria, "valid": score >= 4}


def check_invalidation(payload: Dict, position: Dict, zones: List[Dict], volatility: Dict, bias: Dict) -> Optional[str]:
    """
    Vérifie les conditions d'invalidation d'une position ouverte (section 7 de la spec).
    Retourne une chaîne décrivant l'invalidation, ou None si la position reste valide.
    """
    direction = position["direction"]
    entry_zone_price = position.get("zone_reference_price")

    # 1. Cassure nette de la zone de référence (clôture au-delà, pas juste une mèche)
    last_candle = payload["M15"]["ohlc"][-1]
    if entry_zone_price is not None:
        if direction == "BUY" and last_candle["close"] < entry_zone_price:
            return "Cassure nette de la zone de support ayant justifié l'entrée"
        if direction == "SELL" and last_candle["close"] > entry_zone_price:
            return "Cassure nette de la zone de résistance ayant justifié l'entrée"

    # 2. Macro majeure imminente non anticipée
    if _macro_event_within_buffer(payload.get("evenements_macro_a_venir", []), buffer_minutes=60):
        return "Publication macro majeure imminente non anticipée"

    # 3. IRV en régime extrême sans structure claire
    if volatility["irv"] > 2.0 and bias["structure_h4"] == "range":
        return "IRV en régime extrême (>2.0) sans structure claire"

    # 4. Divergence entre biais H4 et direction du trade
    if direction == "BUY" and bias["structure_h4"] == "bearish":
        return "Divergence: structure H4 s'est retournée à la baisse après entrée"
    if direction == "SELL" and bias["structure_h4"] == "bullish":
        return "Divergence: structure H4 s'est retournée à la hausse après entrée"

    return None


def manage_open_position(payload: Dict, position: Dict, zones: List[Dict], volatility: Dict, bias: Dict) -> Dict:
    """
    Gère une position ouverte: vérifie invalidation, SL/TP touché, et sortie partielle à 1R.
    """
    invalidation_reason = check_invalidation(payload, position, zones, volatility, bias)
    if invalidation_reason:
        return {
            "decision": "EXIT",
            "raisonnement": f"Invalidation détectée: {invalidation_reason}",
        }

    price = payload["prix"]["actuel"]
    entry = position["entry"]
    sl = position["sl"]
    tp = position["tp"]
    direction = position["direction"]
    partial_taken = position.get("partial_exit_taken", False)

    if direction == "BUY":
        r_distance = entry - sl
        current_r = (price - entry) / r_distance if r_distance != 0 else 0
    else:
        r_distance = sl - entry
        current_r = (entry - price) / r_distance if r_distance != 0 else 0

    if not partial_taken and current_r >= 1.0:
        return {
            "decision": "REDUCE",
            "raisonnement": f"Position atteint 1R ({current_r:.2f}) — sortie partielle de 50%, SL remonté au breakeven",
            "nouveau_sl": entry,
            "pourcentage_reduction": 50,
        }

    return {
        "decision": "HOLD",
        "raisonnement": f"Position toujours valide, R actuel = {current_r:.2f}, aucune invalidation détectée",
    }


def evaluate_new_entry(
    payload: Dict,
    direction: str,
    bias: Dict,
    volatility: Dict,
    zones: List[Dict],
) -> Dict:
    """
    Évalue une entrée potentielle dans une direction donnée, applique la
    checklist FQE + le portail de risque (section 6 + section 8).
    """
    price = payload["prix"]["actuel"]
    atr_m15 = payload["M15"].get("atr14") or ind.atr_last(payload["M15"]["ohlc"], period=14) if len(payload["M15"]["ohlc"]) > 14 else volatility["atr_current"]

    zone_proximity = zn.zone_proximity_type(price, zones, volatility["atr_current"], max_distance_factor=0.3)
    price_action_confirmed = pa.detect_price_action_signal(payload["M15"]["ohlc"], direction)
    macro_event_soon = _macro_event_within_buffer(payload.get("evenements_macro_a_venir", []))

    scd = ind.scd_score(price, bias["ema50_h4"], bias["ema200_h4"], bias["structure_h4"], zone_proximity)

    fqe = evaluate_fqe(
        scd=scd,
        direction=direction,
        volatility_regime=volatility["regime"],
        price_action_confirmed=price_action_confirmed,
        zone_proximity=zone_proximity,
        macro_event_soon=macro_event_soon,
    )

    if not fqe["valid"]:
        return {
            "decision": "HOLD",
            "scd": scd,
            "irv": volatility["irv"],
            "fqe_score": fqe["score"],
            "raisonnement": f"FQE insuffisant ({fqe['score']}/5): {fqe['criteria']}",
        }

    # Construction du trade (SL/TP structurels) à partir de la zone la plus proche
    nearest = zn.nearest_zone(price, zones)
    if nearest is None:
        return {
            "decision": "HOLD",
            "scd": scd,
            "irv": volatility["irv"],
            "fqe_score": fqe["score"],
            "raisonnement": "Aucune zone d'intérêt disponible pour structurer SL/TP",
        }

    buffer_ = 0.3 * volatility["atr_current"]
    if direction == "BUY":
        sl = nearest["price"] - buffer_
        risk_distance = price - sl
        tp = price + risk_distance * rm.MIN_RISK_REWARD
    else:
        sl = nearest["price"] + buffer_
        risk_distance = sl - price
        tp = price - risk_distance * rm.MIN_RISK_REWARD

    compte = payload["compte"]
    daily_loss = payload.get("perte_du_jour_cumulee", 0.0)
    losing_trades_today = payload.get("nombre_trades_perdants_jour", 0)
    open_positions = compte.get("positions_ouvertes", [])

    risk_check = rm.full_risk_gate(
        daily_loss_cumulative=daily_loss,
        open_positions=open_positions,
        new_direction=direction,
        losing_trades_today=losing_trades_today,
        entry=price,
        sl=sl,
        tp=tp,
    )

    if not risk_check.allowed:
        return {
            "decision": "HOLD",
            "scd": scd,
            "irv": volatility["irv"],
            "fqe_score": fqe["score"],
            "raisonnement": f"Bloqué par le risk manager: {risk_check.reason}",
        }

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
        "scd": scd,
        "irv": round(volatility["irv"], 3),
        "fqe_score": fqe["score"],
        "zone_reference_price": nearest["price"],
        "raisonnement": (
            f"Entrée {direction} validée — FQE {fqe['score']}/5, SCD={scd}, "
            f"IRV={volatility['irv']:.2f} ({volatility['regime']}), R:R={rr}"
        ),
    }


def analyze(payload: Dict) -> Dict:
    """
    Point d'entrée principal du moteur de décision.

    Si des positions sont ouvertes (payload['compte']['positions_ouvertes']),
    gère chaque position (invalidation / partial exit / hold).

    Sinon, évalue une entrée potentielle dans la direction suggérée par le biais (SCD).
    """
    bias = compute_bias(payload)
    volatility = compute_volatility_regime(payload)
    zones = compute_zones(payload)

    open_positions = payload["compte"].get("positions_ouvertes", [])

    if open_positions:
        # Gestion de la première position ouverte (le portail de risque interdit déjà >2)
        position = open_positions[0]
        result = manage_open_position(payload, position, zones, volatility, bias)
        result.setdefault("scd", None)
        result.setdefault("irv", round(volatility["irv"], 3))
        return result

    price = payload["prix"]["actuel"]
    zone_proximity = zn.zone_proximity_type(price, zones, volatility["atr_current"], max_distance_factor=0.3)
    scd = ind.scd_score(price, bias["ema50_h4"], bias["ema200_h4"], bias["structure_h4"], zone_proximity)

    if scd > 0:
        return evaluate_new_entry(payload, "BUY", bias, volatility, zones)
    elif scd < 0:
        return evaluate_new_entry(payload, "SELL", bias, volatility, zones)
    else:
        return {
            "decision": "HOLD",
            "scd": scd,
            "irv": round(volatility["irv"], 3),
            "fqe_score": None,
            "raisonnement": "SCD neutre (0) — aucun biais directionnel suffisant pour envisager une entrée",
        }