"""
Tests du nouveau decision_engine (Architecture B).

Vérifie:
1. Le dossier d'analyse assemblé contient les bons champs
2. Les garde-fous du risk manager sont appliqués correctement
3. Une décision de Claude qui viole une règle est forcée en HOLD
4. Une décision valide de Claude est complétée avec les métadonnées attendues
"""

import sys
import os
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from trading_engine import decision_engine as de
from trading_engine import risk_manager as rm
from trading_engine import claude_advisor


def make_candle(o, h, l, c):
    return {"open": o, "high": h, "low": l, "close": c}


def flat_candles(n, base=2000.0):
    return [make_candle(base, base + 0.5, base - 0.5, base) for _ in range(n)]


def base_payload(overrides=None):
    d1 = flat_candles(220)
    h4 = flat_candles(220)
    h1 = flat_candles(60)
    m15 = flat_candles(30)
    m5 = flat_candles(30)
    payload = {
        "timestamp": "2026-07-30T10:00:00Z",
        "compte": {
            "solde": 100.0, "equite": 100.0,
            "marge_utilisee": 0.0, "marge_disponible": 100.0,
            "positions_ouvertes": [],
        },
        "prix": {"actuel": 2000.0, "bid": 1999.9, "ask": 2000.1, "spread": 0.2},
        "D1": {"ohlc": d1}, "H4": {"ohlc": h4}, "H1": {"ohlc": h1},
        "M15": {"ohlc": m15}, "M5": {"ohlc": m5},
        "evenements_macro_a_venir": [],
        "declencheur_alerte": "cyclique_30min",
        "perte_du_jour_cumulee": 0.0,
        "nombre_trades_perdants_jour": 0,
    }
    if overrides:
        payload.update(overrides)
    return payload


# --- build_dossier ---

def test_build_dossier_contains_all_required_sections():
    dossier = de.build_dossier(base_payload())
    assert "prix" in dossier
    assert "biais_directionnel" in dossier
    assert "volatilite" in dossier
    assert "zones_interet" in dossier
    assert "action_prix_m15" in dossier
    assert "macro" in dossier
    assert "compte" in dossier
    assert "budget_risque" in dossier


def test_build_dossier_computes_scd():
    dossier = de.build_dossier(base_payload())
    assert isinstance(dossier["biais_directionnel"]["scd"], int)
    assert -3 <= dossier["biais_directionnel"]["scd"] <= 3


def test_build_dossier_computes_irv_regime():
    dossier = de.build_dossier(base_payload())
    assert dossier["volatilite"]["regime"] in ("compression", "normal", "expansion")
    assert dossier["volatilite"]["irv"] > 0


def test_build_dossier_computes_budget_risque():
    payload = base_payload({"perte_du_jour_cumulee": 10.0, "nombre_trades_perdants_jour": 2})
    dossier = de.build_dossier(payload)
    assert dossier["budget_risque"]["perte_max_journaliere_restante"] == 15.0
    assert dossier["budget_risque"]["trades_perdants_restants"] == 3


def test_build_dossier_budget_never_negative():
    """Si la perte du jour dépasse le plafond, le restant doit être 0, pas négatif."""
    payload = base_payload({"perte_du_jour_cumulee": 40.0})
    dossier = de.build_dossier(payload)
    assert dossier["budget_risque"]["perte_max_journaliere_restante"] == 0


# --- apply_risk_guardrails : HOLD/EXIT passent tels quels ---

def test_apply_risk_guardrails_passes_hold_through():
    claude_decision = {
        "decision": "HOLD", "direction": None,
        "sl_propose": None, "tp_propose": None,
        "raisonnement": "contexte défavorable", "confiance": "haute",
        "risques_identifies": [],
    }
    result = de.apply_risk_guardrails(claude_decision, base_payload(), {})
    assert result["decision"] == "HOLD"
    assert result["raisonnement"] == "contexte défavorable"


def test_apply_risk_guardrails_passes_exit_through():
    claude_decision = {
        "decision": "EXIT", "direction": None,
        "sl_propose": None, "tp_propose": None,
        "raisonnement": "invalidation détectée", "confiance": "haute",
        "risques_identifies": [],
    }
    result = de.apply_risk_guardrails(claude_decision, base_payload(), {})
    assert result["decision"] == "EXIT"


def test_apply_risk_guardrails_passes_reduce_with_new_sl():
    claude_decision = {
        "decision": "REDUCE", "direction": None,
        "sl_propose": 2000.0, "tp_propose": None,
        "raisonnement": "1R atteint", "confiance": "haute",
        "risques_identifies": [],
    }
    result = de.apply_risk_guardrails(claude_decision, base_payload(), {})
    assert result["decision"] == "REDUCE"
    assert result["nouveau_sl"] == 2000.0
    assert result["pourcentage_reduction"] == 50


# --- apply_risk_guardrails : ENTER validé ---

def test_apply_risk_guardrails_accepts_valid_enter():
    claude_decision = {
        "decision": "ENTER", "direction": "BUY",
        "sl_propose": 1995.0, "tp_propose": 2010.0,
        "raisonnement": "setup complet", "confiance": "haute",
        "risques_identifies": ["macro dans 90 min"],
    }
    result = de.apply_risk_guardrails(claude_decision, base_payload(), {})
    assert result["decision"] == "ENTER"
    assert result["direction"] == "BUY"
    assert result["sl"] == 1995.0
    assert result["tp"] == 2010.0
    assert result["rr_vise"] >= 2.0
    assert result["risque_dollars"] == 5.0  # forcé au minimum


# --- apply_risk_guardrails : ENTER bloqué ---

def test_apply_risk_guardrails_blocks_enter_when_daily_loss_reached():
    payload = base_payload({"perte_du_jour_cumulee": 25.0})
    claude_decision = {
        "decision": "ENTER", "direction": "BUY",
        "sl_propose": 1995.0, "tp_propose": 2010.0,
        "raisonnement": "mais je veux entrer", "confiance": "haute",
        "risques_identifies": [],
    }
    result = de.apply_risk_guardrails(claude_decision, payload, {})
    assert result["decision"] == "HOLD"
    assert "risk manager" in result["raisonnement"].lower()
    assert "Perte journalière" in result["raisonnement"]


def test_apply_risk_guardrails_blocks_enter_when_max_losing_trades():
    payload = base_payload({"nombre_trades_perdants_jour": 5})
    claude_decision = {
        "decision": "ENTER", "direction": "BUY",
        "sl_propose": 1995.0, "tp_propose": 2010.0,
        "raisonnement": "reprise possible", "confiance": "haute",
        "risques_identifies": [],
    }
    result = de.apply_risk_guardrails(claude_decision, payload, {})
    assert result["decision"] == "HOLD"


def test_apply_risk_guardrails_blocks_enter_when_rr_below_2():
    claude_decision = {
        "decision": "ENTER", "direction": "BUY",
        "sl_propose": 1995.0, "tp_propose": 2005.0,  # R:R = 1.0, insuffisant
        "raisonnement": "petite entrée", "confiance": "moyenne",
        "risques_identifies": [],
    }
    result = de.apply_risk_guardrails(claude_decision, base_payload(), {})
    assert result["decision"] == "HOLD"
    assert "R:R" in result["raisonnement"]


def test_apply_risk_guardrails_blocks_enter_when_correlated_position():
    payload = base_payload({
        "compte": {
            "solde": 100.0, "equite": 100.0,
            "marge_utilisee": 5.0, "marge_disponible": 95.0,
            "positions_ouvertes": [{"direction": "BUY", "trade_id": "101"}],
        }
    })
    claude_decision = {
        "decision": "ENTER", "direction": "BUY",  # même sens que la position ouverte
        "sl_propose": 1995.0, "tp_propose": 2010.0,
        "raisonnement": "renforcement", "confiance": "haute",
        "risques_identifies": [],
    }
    result = de.apply_risk_guardrails(claude_decision, payload, {})
    assert result["decision"] == "HOLD"
    assert "corr" in result["raisonnement"].lower()


def test_apply_risk_guardrails_blocks_enter_when_max_positions():
    payload = base_payload({
        "compte": {
            "solde": 100.0, "equite": 100.0,
            "marge_utilisee": 10.0, "marge_disponible": 90.0,
            "positions_ouvertes": [
                {"direction": "BUY", "trade_id": "101"},
                {"direction": "SELL", "trade_id": "102"},
            ],
        }
    })
    claude_decision = {
        "decision": "ENTER", "direction": "BUY",
        "sl_propose": 1995.0, "tp_propose": 2010.0,
        "raisonnement": "3e position", "confiance": "haute",
        "risques_identifies": [],
    }
    result = de.apply_risk_guardrails(claude_decision, payload, {})
    assert result["decision"] == "HOLD"


def test_apply_risk_guardrails_blocks_incomplete_enter():
    """ENTER sans direction/sl/tp doit être forcé en HOLD."""
    claude_decision = {
        "decision": "ENTER", "direction": None,
        "sl_propose": None, "tp_propose": None,
        "raisonnement": "j'ai oublié les détails", "confiance": "basse",
        "risques_identifies": [],
    }
    result = de.apply_risk_guardrails(claude_decision, base_payload(), {})
    assert result["decision"] == "HOLD"


# --- analyze : orchestration complète avec Claude mocké ---

def test_analyze_calls_claude_and_returns_final_decision(monkeypatch):
    def fake_ask_claude(dossier, client=None):
        return {
            "decision": "HOLD", "direction": None,
            "sl_propose": None, "tp_propose": None,
            "raisonnement": "test", "confiance": "moyenne",
            "risques_identifies": [],
        }
    monkeypatch.setattr(claude_advisor, "ask_claude", fake_ask_claude)

    result = de.analyze(base_payload())
    assert result["decision"] == "HOLD"
    assert "scd" in result
    assert "irv" in result


def test_analyze_receives_dossier_with_expected_fields(monkeypatch):
    captured = {}

    def fake_ask_claude(dossier, client=None):
        captured["dossier"] = dossier
        return {
            "decision": "HOLD", "direction": None,
            "sl_propose": None, "tp_propose": None,
            "raisonnement": "test", "confiance": "moyenne",
            "risques_identifies": [],
        }
    monkeypatch.setattr(claude_advisor, "ask_claude", fake_ask_claude)

    de.analyze(base_payload())
    assert "prix" in captured["dossier"]
    assert "biais_directionnel" in captured["dossier"]
    assert "budget_risque" in captured["dossier"]


def test_analyze_forces_hold_when_claude_returns_invalid_enter(monkeypatch):
    """Si Claude retourne ENTER mais viole une règle, l'analyse doit forcer HOLD."""
    def fake_ask_claude(dossier, client=None):
        return {
            "decision": "ENTER", "direction": "BUY",
            "sl_propose": 1999.5, "tp_propose": 2000.5,  # R:R = 1.0, trop bas
            "raisonnement": "tentative", "confiance": "haute",
            "risques_identifies": [],
        }
    monkeypatch.setattr(claude_advisor, "ask_claude", fake_ask_claude)

    result = de.analyze(base_payload())
    assert result["decision"] == "HOLD"


def test_analyze_handles_dossier_build_error(monkeypatch):
    """Si build_dossier plante, on doit avoir un HOLD par défaut."""
    def failing_build(payload):
        raise RuntimeError("erreur simulée dans build_dossier")
    monkeypatch.setattr(de, "build_dossier", failing_build)

    result = de.analyze(base_payload())
    assert result["decision"] == "HOLD"
    assert "technique" in result["raisonnement"].lower() or "erreur" in result["raisonnement"].lower()
