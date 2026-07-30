"""
risk_manager.py

Gestion du risque — paramètres non négociables du système (section 8 de la spec).

RÈGLES FIGÉES (ne pas modifier sans validation explicite du gestionnaire):
- Risque minimum par trade: 5$ (5% d'un capital de 100$)
- Perte maximale journalière: 25$ (25%)
- Pas de plafond de perte cumulée sur 7 jours (budget rechargé chaque jour)
- Maximum 2 positions simultanées
- R:R minimum exigé: 2.0
- Maximum 5 trades perdants par jour (25$ / 5$)
"""

from dataclasses import dataclass
from typing import List, Dict, Optional


# --- Constantes de risque (figées par la spec) ---
RISK_PER_TRADE_MIN_DOLLARS = 5.0
DAILY_LOSS_MAX_DOLLARS = 25.0
DAILY_LOSS_ALERT_THRESHOLD_DOLLARS = 20.0
MAX_OPEN_POSITIONS = 2
MIN_RISK_REWARD = 2.0
MAX_LOSING_TRADES_PER_DAY = 5


@dataclass
class RiskCheckResult:
    allowed: bool
    reason: str


def check_daily_loss_limit(daily_loss_cumulative: float) -> RiskCheckResult:
    """
    Vérifie si le budget de perte journalier autorise encore de nouvelles entrées.
    `daily_loss_cumulative` doit être un nombre positif représentant les pertes du jour en $.
    """
    if daily_loss_cumulative >= DAILY_LOSS_MAX_DOLLARS:
        return RiskCheckResult(
            allowed=False,
            reason=f"Perte journalière ({daily_loss_cumulative}$) a atteint le plafond de {DAILY_LOSS_MAX_DOLLARS}$. "
                    f"Arrêt des entrées jusqu'au lendemain.",
        )
    return RiskCheckResult(allowed=True, reason="Budget de perte journalier respecté")


def is_daily_loss_alert(daily_loss_cumulative: float) -> bool:
    """Retourne True si le seuil d'alerte (20$) est atteint mais pas encore le plafond (25$)."""
    return DAILY_LOSS_ALERT_THRESHOLD_DOLLARS <= daily_loss_cumulative < DAILY_LOSS_MAX_DOLLARS


def check_max_positions(open_positions: List[Dict]) -> RiskCheckResult:
    """Vérifie que le nombre de positions ouvertes ne dépasse pas le maximum autorisé."""
    if len(open_positions) >= MAX_OPEN_POSITIONS:
        return RiskCheckResult(
            allowed=False,
            reason=f"{len(open_positions)} positions déjà ouvertes (max {MAX_OPEN_POSITIONS})",
        )
    return RiskCheckResult(allowed=True, reason="Nombre de positions ouvertes OK")


def check_correlated_exposure(open_positions: List[Dict], new_direction: str) -> RiskCheckResult:
    """
    Empêche l'ouverture d'une nouvelle position dans la même direction qu'une
    position déjà ouverte (sur-exposition corrélée sur le même actif).
    """
    same_direction = [p for p in open_positions if p.get("direction") == new_direction]
    if same_direction:
        return RiskCheckResult(
            allowed=False,
            reason=f"Une position {new_direction} est déjà ouverte — exposition corrélée refusée",
        )
    return RiskCheckResult(allowed=True, reason="Pas de corrélation directionnelle")


def check_losing_trades_count(losing_trades_today: int) -> RiskCheckResult:
    """Vérifie que le nombre de trades perdants du jour ne dépasse pas le maximum."""
    if losing_trades_today >= MAX_LOSING_TRADES_PER_DAY:
        return RiskCheckResult(
            allowed=False,
            reason=f"{losing_trades_today} trades perdants aujourd'hui (max {MAX_LOSING_TRADES_PER_DAY})",
        )
    return RiskCheckResult(allowed=True, reason="Compteur de trades perdants OK")


def calculate_position_size(
    risk_dollars: float,
    sl_distance_price: float,
    pip_value_per_lot: float,
    pip_size: float = 0.1,
) -> float:
    """
    Calcule la taille de position (en lots) à partir du risque en dollars fixé
    et de la distance du SL en unités de prix.

    Sur XAUUSD, une convention courante: 1 pip = 0.10 (variable selon le broker),
    pip_value_per_lot = valeur en $ d'un pip pour 1.0 lot standard (à fournir par le broker).

    Taille = Risque$ / (Distance_SL_en_pips * valeur_du_pip_par_lot)
    """
    if sl_distance_price <= 0:
        raise ValueError("sl_distance_price doit être > 0")
    if pip_value_per_lot <= 0:
        raise ValueError("pip_value_per_lot doit être > 0")

    sl_distance_pips = sl_distance_price / pip_size
    position_size_lots = risk_dollars / (sl_distance_pips * pip_value_per_lot)
    return round(position_size_lots, 3)


def enforce_min_risk(requested_risk_dollars: float) -> float:
    """Force le risque par trade à respecter le minimum de 5$ défini par la spec."""
    return max(requested_risk_dollars, RISK_PER_TRADE_MIN_DOLLARS)


def compute_risk_reward(entry: float, sl: float, tp: float, direction: str) -> float:
    """Calcule le ratio Risque:Rendement (R:R) d'un trade projeté."""
    if direction == "BUY":
        risk = entry - sl
        reward = tp - entry
    elif direction == "SELL":
        risk = sl - entry
        reward = entry - tp
    else:
        raise ValueError("direction doit être 'BUY' ou 'SELL'")

    if risk <= 0:
        raise ValueError("SL invalide: le risque calculé doit être positif (SL du mauvais côté de l'entrée)")
    if reward <= 0:
        raise ValueError("TP invalide: le reward calculé doit être positif (TP du mauvais côté de l'entrée)")

    return round(reward / risk, 3)


def check_risk_reward(entry: float, sl: float, tp: float, direction: str) -> RiskCheckResult:
    """Vérifie que le R:R d'un trade projeté respecte le minimum requis (2.0)."""
    rr = compute_risk_reward(entry, sl, tp, direction)
    if rr < MIN_RISK_REWARD:
        return RiskCheckResult(
            allowed=False,
            reason=f"R:R calculé ({rr}) < minimum requis ({MIN_RISK_REWARD})",
        )
    return RiskCheckResult(allowed=True, reason=f"R:R calculé ({rr}) conforme")


def check_spread(spread: float, risk_dollars: float, spread_cost_per_unit: float, max_spread_pct_of_risk: float = 0.15) -> RiskCheckResult:
    """
    Vérifie que le coût du spread reste sous un pourcentage acceptable du risque du trade.
    `spread_cost_per_unit`: coût en $ du spread actuel pour la taille de position considérée.
    """
    max_acceptable_cost = risk_dollars * max_spread_pct_of_risk
    if spread_cost_per_unit > max_acceptable_cost:
        return RiskCheckResult(
            allowed=False,
            reason=f"Coût du spread ({spread_cost_per_unit}$) dépasse {max_spread_pct_of_risk*100}% du risque ({max_acceptable_cost}$)",
        )
    return RiskCheckResult(allowed=True, reason="Spread acceptable")


def full_risk_gate(
    daily_loss_cumulative: float,
    open_positions: List[Dict],
    new_direction: str,
    losing_trades_today: int,
    entry: float,
    sl: float,
    tp: float,
) -> RiskCheckResult:
    """
    Portail de risque global: toute entrée doit passer TOUTES ces vérifications.
    Retourne le premier échec rencontré, ou allowed=True si tout est validé.
    """
    checks = [
        check_daily_loss_limit(daily_loss_cumulative),
        check_max_positions(open_positions),
        check_correlated_exposure(open_positions, new_direction),
        check_losing_trades_count(losing_trades_today),
        check_risk_reward(entry, sl, tp, new_direction),
    ]
    for check in checks:
        if not check.allowed:
            return check
    return RiskCheckResult(allowed=True, reason="Toutes les vérifications de risque sont passées")