import sys
import os
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from trading_engine import risk_manager as rm


# --- Perte journalière ---

def test_daily_loss_limit_allows_under_threshold():
    result = rm.check_daily_loss_limit(10.0)
    assert result.allowed is True


def test_daily_loss_limit_blocks_at_exact_threshold():
    result = rm.check_daily_loss_limit(25.0)
    assert result.allowed is False


def test_daily_loss_limit_blocks_above_threshold():
    result = rm.check_daily_loss_limit(30.0)
    assert result.allowed is False


def test_daily_loss_alert_zone():
    assert rm.is_daily_loss_alert(20.0) is True
    assert rm.is_daily_loss_alert(22.5) is True
    assert rm.is_daily_loss_alert(19.99) is False
    assert rm.is_daily_loss_alert(25.0) is False  # déjà au plafond, plus une "alerte"


# --- Positions max ---

def test_max_positions_allows_below_limit():
    result = rm.check_max_positions([{"direction": "BUY"}])
    assert result.allowed is True


def test_max_positions_blocks_at_limit():
    result = rm.check_max_positions([{"direction": "BUY"}, {"direction": "SELL"}])
    assert result.allowed is False


def test_max_positions_allows_zero_positions():
    result = rm.check_max_positions([])
    assert result.allowed is True


# --- Exposition corrélée ---

def test_correlated_exposure_blocks_same_direction():
    result = rm.check_correlated_exposure([{"direction": "BUY"}], "BUY")
    assert result.allowed is False


def test_correlated_exposure_allows_opposite_direction():
    result = rm.check_correlated_exposure([{"direction": "BUY"}], "SELL")
    assert result.allowed is True


def test_correlated_exposure_allows_no_positions():
    result = rm.check_correlated_exposure([], "BUY")
    assert result.allowed is True


# --- Trades perdants du jour ---

def test_losing_trades_count_allows_below_max():
    result = rm.check_losing_trades_count(4)
    assert result.allowed is True


def test_losing_trades_count_blocks_at_max():
    result = rm.check_losing_trades_count(5)
    assert result.allowed is False


# --- Position sizing ---

def test_calculate_position_size_basic():
    # Risque 5$, SL à 5.0 unités de prix, pip_size=0.1 -> 50 pips, pip_value=1$/pip/lot
    size = rm.calculate_position_size(risk_dollars=5.0, sl_distance_price=5.0, pip_value_per_lot=1.0, pip_size=0.1)
    # 50 pips * 1$ = 50$/lot de risque -> taille = 5/50 = 0.1 lot
    assert size == pytest.approx(0.1)


def test_calculate_position_size_invalid_sl_raises():
    with pytest.raises(ValueError):
        rm.calculate_position_size(risk_dollars=5.0, sl_distance_price=0, pip_value_per_lot=1.0)


def test_calculate_position_size_invalid_pip_value_raises():
    with pytest.raises(ValueError):
        rm.calculate_position_size(risk_dollars=5.0, sl_distance_price=5.0, pip_value_per_lot=0)


# --- Risque minimum ---

def test_enforce_min_risk_raises_low_value_to_minimum():
    assert rm.enforce_min_risk(2.0) == 5.0


def test_enforce_min_risk_keeps_higher_value():
    assert rm.enforce_min_risk(8.0) == 8.0


def test_enforce_min_risk_keeps_exact_minimum():
    assert rm.enforce_min_risk(5.0) == 5.0


# --- R:R ---

def test_compute_risk_reward_buy():
    rr = rm.compute_risk_reward(entry=2000, sl=1995, tp=2010, direction="BUY")
    assert rr == pytest.approx(2.0)


def test_compute_risk_reward_sell():
    rr = rm.compute_risk_reward(entry=2000, sl=2005, tp=1990, direction="SELL")
    assert rr == pytest.approx(2.0)


def test_compute_risk_reward_invalid_sl_side_raises():
    with pytest.raises(ValueError):
        rm.compute_risk_reward(entry=2000, sl=2005, tp=2010, direction="BUY")  # SL du mauvais côté


def test_compute_risk_reward_invalid_tp_side_raises():
    with pytest.raises(ValueError):
        rm.compute_risk_reward(entry=2000, sl=1995, tp=1990, direction="BUY")  # TP du mauvais côté


def test_check_risk_reward_blocks_below_minimum():
    result = rm.check_risk_reward(entry=2000, sl=1995, tp=2005, direction="BUY")  # RR = 1.0
    assert result.allowed is False


def test_check_risk_reward_allows_at_minimum():
    result = rm.check_risk_reward(entry=2000, sl=1995, tp=2010, direction="BUY")  # RR = 2.0
    assert result.allowed is True


# --- Spread ---

def test_check_spread_allows_acceptable_cost():
    result = rm.check_spread(spread=0.5, risk_dollars=5.0, spread_cost_per_unit=0.5, max_spread_pct_of_risk=0.15)
    assert result.allowed is True


def test_check_spread_blocks_excessive_cost():
    result = rm.check_spread(spread=2.0, risk_dollars=5.0, spread_cost_per_unit=2.0, max_spread_pct_of_risk=0.15)
    assert result.allowed is False


# --- Portail global ---

def test_full_risk_gate_passes_all_checks():
    result = rm.full_risk_gate(
        daily_loss_cumulative=10.0,
        open_positions=[],
        new_direction="BUY",
        losing_trades_today=1,
        entry=2000,
        sl=1995,
        tp=2010,
    )
    assert result.allowed is True


def test_full_risk_gate_blocks_on_daily_loss_first():
    result = rm.full_risk_gate(
        daily_loss_cumulative=25.0,
        open_positions=[],
        new_direction="BUY",
        losing_trades_today=1,
        entry=2000,
        sl=1995,
        tp=2010,
    )
    assert result.allowed is False
    assert "Perte journalière" in result.reason


def test_full_risk_gate_blocks_on_low_rr_when_other_checks_pass():
    result = rm.full_risk_gate(
        daily_loss_cumulative=0.0,
        open_positions=[],
        new_direction="BUY",
        losing_trades_today=0,
        entry=2000,
        sl=1995,
        tp=2003,  # RR = 0.6, insuffisant
    )
    assert result.allowed is False
    assert "R:R" in result.reason