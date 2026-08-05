"""
risk_manager.py

Gestion du risque — paramètres non négociables du système MVP.

RÈGLES FIGÉES :
- Perte maximale journalière : 50 $ (kill switch dur)
- Une seule position à la fois (spec MVP)
- Pas de pyramiding
- R:R minimum : 1.5
- Max 5 trades perdants par jour (50 $ / 10 $ par trade)
"""

from dataclasses import dataclass
from typing import List, Dict

# Import config pour lire DAILY_LOSS_MAX depuis .env si présent
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402


# --- Constantes de risque ---
RISK_PER_TRADE_MIN_DOLLARS = 5.0
DAILY_LOSS_MAX_DOLLARS = config.DAILY_LOSS_MAX_DOLLARS  # 50 par défaut
DAILY_LOSS_ALERT_THRESHOLD_DOLLARS = DAILY_LOSS_MAX_DOLLARS * 0.8
MAX_OPEN_POSITIONS = config.MAX_OPEN_POSITIONS         # 1 (MVP)
MIN_RISK_REWARD = config.MIN_RISK_REWARD               # 1.5
MAX_LOSING_TRADES_PER_DAY = 5


@dataclass
class RiskCheckResult:
    allowed: bool
    reason: str


def check_daily_loss_limit(daily_loss_cumulative: float) -> RiskCheckResult:
    if daily_loss_cumulative >= DAILY_LOSS_MAX_DOLLARS:
        return RiskCheckResult(
            allowed=False,
            reason=f"Perte journalière ({daily_loss_cumulative}$) a atteint le plafond de "
                   f"{DAILY_LOSS_MAX_DOLLARS}$. Arrêt des entrées jusqu'au lendemain.",
        )
    return RiskCheckResult(allowed=True, reason="Budget de perte journalier respecté")


def is_daily_loss_alert(daily_loss_cumulative: float) -> bool:
    return DAILY_LOSS_ALERT_THRESHOLD_DOLLARS <= daily_loss_cumulative < DAILY_LOSS_MAX_DOLLARS


def check_max_positions(open_positions: List[Dict]) -> RiskCheckResult:
    if len(open_positions) >= MAX_OPEN_POSITIONS:
        return RiskCheckResult(
            allowed=False,
            reason=f"{len(open_positions)} position(s) déjà ouverte(s) (max {MAX_OPEN_POSITIONS})",
        )
    return RiskCheckResult(allowed=True, reason="Nombre de positions ouvertes OK")


def check_losing_trades_count(losing_trades_today: int) -> RiskCheckResult:
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
    Sur XAUUSD : 1 pip = 0.10, contract = 100 oz.
    Valeur d'un pip pour 1 lot = 100 × 0.10 = 10 $.
    Taille = Risque$ / (Distance_SL_en_pips × valeur_pip_par_lot)
    """
    if sl_distance_price <= 0:
        raise ValueError("sl_distance_price doit être > 0")
    if pip_value_per_lot <= 0:
        raise ValueError("pip_value_per_lot doit être > 0")
    sl_distance_pips = sl_distance_price / pip_size
    return round(risk_dollars / (sl_distance_pips * pip_value_per_lot), 3)


def enforce_min_risk(requested_risk_dollars: float) -> float:
    return max(requested_risk_dollars, RISK_PER_TRADE_MIN_DOLLARS)


def compute_risk_reward(entry: float, sl: float, tp: float, direction: str) -> float:
    if direction == "BUY":
        risk, reward = entry - sl, tp - entry
    elif direction == "SELL":
        risk, reward = sl - entry, entry - tp
    else:
        raise ValueError("direction doit être 'BUY' ou 'SELL'")
    if risk <= 0:
        raise ValueError("SL invalide : risque calculé doit être > 0")
    if reward <= 0:
        raise ValueError("TP invalide : reward calculé doit être > 0")
    return round(reward / risk, 3)


def check_risk_reward(entry: float, sl: float, tp: float, direction: str) -> RiskCheckResult:
    rr = compute_risk_reward(entry, sl, tp, direction)
    if rr < MIN_RISK_REWARD:
        return RiskCheckResult(
            allowed=False,
            reason=f"R:R calculé ({rr}) < minimum requis ({MIN_RISK_REWARD})",
        )
    return RiskCheckResult(allowed=True, reason=f"R:R calculé ({rr}) conforme")


def full_risk_gate(
    daily_loss_cumulative: float,
    open_positions: List[Dict],
    losing_trades_today: int,
    entry: float,
    sl: float,
    tp: float,
    direction: str,
) -> RiskCheckResult:
    checks = [
        check_daily_loss_limit(daily_loss_cumulative),
        check_max_positions(open_positions),
        check_losing_trades_count(losing_trades_today),
        check_risk_reward(entry, sl, tp, direction),
    ]
    for check in checks:
        if not check.allowed:
            return check
    return RiskCheckResult(allowed=True, reason="Toutes les vérifications de risque sont passées")
