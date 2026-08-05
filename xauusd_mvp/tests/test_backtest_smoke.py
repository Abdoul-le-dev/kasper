"""Smoke test end-to-end : dataset synthétique → backtest → rapport.

Vérifie qu'on peut faire tourner les 3 stratégies sans crash et que le
rapport HTML est bien généré.
"""

from datetime import datetime, timezone, timedelta
import numpy as np
import polars as pl
import pytest
import tempfile
from pathlib import Path

from src.strategies.supertrend_atr import SupertrendAtrStrategy, SupertrendAtrConfig
from src.strategies.hull_vwap import HullVwapStrategy, HullVwapConfig
from src.strategies.ttm_adx import TtmAdxStrategy, TtmAdxConfig
from src.backtest import run_backtest
from src.report import render_report


def make_synthetic_m5(n_bars=1000, seed=7):
    """OHLC M5 avec quelques flips de tendance intentionnels."""
    rng = np.random.default_rng(seed)
    # 5 régimes alternés haussier/baissier
    segments = np.tile([0.5, -0.5, 0.3, -0.3, 0.4], n_bars // 5 + 1)[:n_bars]
    close = 2000.0 + np.cumsum(segments + rng.normal(0, 0.5, n_bars))
    high = close + np.abs(rng.normal(0, 0.4, n_bars))
    low = close - np.abs(rng.normal(0, 0.4, n_bars))
    open_ = np.roll(close, 1); open_[0] = 2000.0
    volume = rng.integers(100, 500, n_bars).astype(float)
    spread = rng.uniform(0.3, 0.8, n_bars)
    # Timestamps M5 démarrant lundi 07:00 UTC (session Londres)
    start = datetime(2024, 6, 3, 7, 0, tzinfo=timezone.utc)
    ts = [start + timedelta(minutes=5 * i) for i in range(n_bars)]
    return pl.DataFrame({
        "ts": ts,
        "open": open_, "high": high, "low": low, "close": close,
        "tick_volume": volume, "spread_avg": spread,
    })


def make_synthetic_h1_from_m5(df_m5):
    """Resample M5 → H1 pour le contexte."""
    return (
        df_m5.sort("ts")
        .group_by_dynamic("ts", every="1h", closed="left", label="left")
        .agg([
            pl.col("open").first().alias("open"),
            pl.col("high").max().alias("high"),
            pl.col("low").min().alias("low"),
            pl.col("close").last().alias("close"),
            pl.col("tick_volume").sum().alias("tick_volume"),
            pl.col("spread_avg").mean().alias("spread_avg"),
        ])
    )


@pytest.mark.parametrize("strategy_factory,cfg_factory", [
    (SupertrendAtrStrategy, lambda: SupertrendAtrConfig(name="test_st")),
    (HullVwapStrategy, lambda: HullVwapConfig(name="test_hv")),
    (TtmAdxStrategy, lambda: TtmAdxConfig(name="test_ttm")),
])
def test_backtest_end_to_end(strategy_factory, cfg_factory):
    df_m5 = make_synthetic_m5(1000)
    df_h1 = make_synthetic_h1_from_m5(df_m5)
    strat = strategy_factory(cfg_factory())
    result = run_backtest(strat, df_m5, df_h1)
    # Doit produire un résultat structuré sans crash
    assert result.strategy_name is not None
    assert result.n_trades >= 0
    # Peut être 0 trades sur données synthétiques : c'est OK
    # On teste juste que le pipeline ne crash pas


def test_report_html_generated():
    df_m5 = make_synthetic_m5(1000)
    df_h1 = make_synthetic_h1_from_m5(df_m5)
    strat = SupertrendAtrStrategy(SupertrendAtrConfig(name="test"))
    result = run_backtest(strat, df_m5, df_h1)
    with tempfile.NamedTemporaryFile(suffix=".html", delete=False) as f:
        out = Path(f.name)
    render_report(result, out)
    content = out.read_text()
    assert "<!DOCTYPE html>" in content
    assert "Backtest" in content
    out.unlink()
