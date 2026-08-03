"""
Tests isolés du module journal.py — utilise des fichiers temporaires (tmp_path
de pytest) pour ne jamais toucher aux vrais fichiers de données du système.
"""

import sys
import os
import json
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from trading_engine import journal


def test_log_decision_writes_jsonl_line(tmp_path):
    journal_path = str(tmp_path / "journal.jsonl")
    payload = {"prix": {"actuel": 2000.0}, "perte_du_jour_cumulee": 5.0, "nombre_trades_perdants_jour": 1, "declencheur_alerte": "cyclique_30min"}
    decision = {"decision": "HOLD", "raisonnement": "test"}

    journal.log_decision(payload, decision, journal_path=journal_path)

    with open(journal_path) as f:
        lines = f.readlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["decision"]["decision"] == "HOLD"
    assert entry["input_summary"]["prix_actuel"] == 2000.0


def test_log_decision_appends_multiple_entries(tmp_path):
    journal_path = str(tmp_path / "journal.jsonl")
    payload = {"prix": {"actuel": 2000.0}}
    for i in range(3):
        journal.log_decision(payload, {"decision": "HOLD"}, journal_path=journal_path)

    entries = journal.read_journal(journal_path=journal_path)
    assert len(entries) == 3


def test_read_journal_empty_when_file_missing(tmp_path):
    journal_path = str(tmp_path / "does_not_exist.jsonl")
    assert journal.read_journal(journal_path=journal_path) == []


def test_read_journal_respects_limit(tmp_path):
    journal_path = str(tmp_path / "journal.jsonl")
    for i in range(5):
        journal.log_decision({"prix": {"actuel": i}}, {"decision": "HOLD"}, journal_path=journal_path)

    entries = journal.read_journal(journal_path=journal_path, limit=2)
    assert len(entries) == 2


# --- Métadonnées de position ---

def test_save_and_get_position_metadata(tmp_path):
    path = str(tmp_path / "positions_meta.json")
    journal.save_position_metadata("trade-1", {"zone_reference_price": 1995.0, "partial_exit_taken": False}, path=path)

    meta = journal.get_position_metadata("trade-1", path=path)
    assert meta["zone_reference_price"] == 1995.0
    assert meta["partial_exit_taken"] is False


def test_get_position_metadata_returns_none_when_missing(tmp_path):
    path = str(tmp_path / "positions_meta.json")
    assert journal.get_position_metadata("unknown-trade", path=path) is None


def test_delete_position_metadata_removes_entry(tmp_path):
    path = str(tmp_path / "positions_meta.json")
    journal.save_position_metadata("trade-1", {"zone_reference_price": 1995.0}, path=path)
    journal.delete_position_metadata("trade-1", path=path)
    assert journal.get_position_metadata("trade-1", path=path) is None


def test_save_position_metadata_preserves_other_entries(tmp_path):
    path = str(tmp_path / "positions_meta.json")
    journal.save_position_metadata("trade-1", {"zone_reference_price": 1995.0}, path=path)
    journal.save_position_metadata("trade-2", {"zone_reference_price": 2005.0}, path=path)

    assert journal.get_position_metadata("trade-1", path=path)["zone_reference_price"] == 1995.0
    assert journal.get_position_metadata("trade-2", path=path)["zone_reference_price"] == 2005.0


# --- État journalier ---

def test_get_daily_state_defaults_when_missing(tmp_path):
    path = str(tmp_path / "daily_state.json")
    state = journal.get_daily_state("2026-07-30", path=path)
    assert state["perte_du_jour_cumulee"] == 0.0
    assert state["nombre_trades_perdants_jour"] == 0


def test_update_daily_state_accumulates_loss(tmp_path):
    path = str(tmp_path / "daily_state.json")
    journal.update_daily_state("2026-07-30", loss_delta=5.0, is_losing_trade=True, path=path)
    journal.update_daily_state("2026-07-30", loss_delta=5.0, is_losing_trade=True, path=path)

    state = journal.get_daily_state("2026-07-30", path=path)
    assert state["perte_du_jour_cumulee"] == 10.0
    assert state["nombre_trades_perdants_jour"] == 2


def test_update_daily_state_ignores_negative_loss_delta(tmp_path):
    """Un gain (delta négatif de 'perte') ne doit jamais réduire le compteur de perte cumulée."""
    path = str(tmp_path / "daily_state.json")
    journal.update_daily_state("2026-07-30", loss_delta=-3.0, is_losing_trade=False, path=path)
    state = journal.get_daily_state("2026-07-30", path=path)
    assert state["perte_du_jour_cumulee"] == 0.0
    assert state["nombre_trades_perdants_jour"] == 0


def test_daily_state_isolated_per_date(tmp_path):
    path = str(tmp_path / "daily_state.json")
    journal.update_daily_state("2026-07-30", loss_delta=5.0, is_losing_trade=True, path=path)
    journal.update_daily_state("2026-07-31", loss_delta=8.0, is_losing_trade=True, path=path)

    state_30 = journal.get_daily_state("2026-07-30", path=path)
    state_31 = journal.get_daily_state("2026-07-31", path=path)
    assert state_30["perte_du_jour_cumulee"] == 5.0
    assert state_31["perte_du_jour_cumulee"] == 8.0
