"""
journal.py

Journalisation persistante de chaque décision (section 12 de la spec), et
mémorisation des métadonnées stratégiques que le broker ne connaît pas
(zone_reference_price, partial_exit_taken) — nécessaires pour gérer
correctement l'invalidation et la sortie partielle à 1R sur les prochains cycles.

Stockage : fichier JSONL local (une ligne JSON par décision), append-only,
simple et suffisant pour 7 jours d'opération. Peut être remplacé par une
vraie base de données plus tard sans changer l'API de ce module.
"""

import json
import os
import threading
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any

_LOCK = threading.Lock()

DEFAULT_JOURNAL_PATH = os.environ.get("JOURNAL_PATH", "/home/claude/trading_engine/data/journal.jsonl")
DEFAULT_POSITIONS_META_PATH = os.environ.get("POSITIONS_META_PATH", "/home/claude/trading_engine/data/positions_meta.json")


def _ensure_dir(path: str) -> None:
    directory = os.path.dirname(path)
    if directory and not os.path.exists(directory):
        os.makedirs(directory, exist_ok=True)


def log_decision(payload_in: Dict[str, Any], decision_out: Dict[str, Any], journal_path: str = DEFAULT_JOURNAL_PATH) -> None:
    """
    Enregistre une décision complète (entrée + sortie du moteur) avec horodatage.
    Append-only, thread-safe.
    """
    _ensure_dir(journal_path)
    entry = {
        "logged_at": datetime.now(timezone.utc).isoformat(),
        "input_summary": {
            "prix_actuel": payload_in.get("prix", {}).get("actuel"),
            "perte_du_jour_cumulee": payload_in.get("perte_du_jour_cumulee"),
            "nombre_trades_perdants_jour": payload_in.get("nombre_trades_perdants_jour"),
            "declencheur_alerte": payload_in.get("declencheur_alerte"),
        },
        "decision": decision_out,
    }
    with _LOCK:
        with open(journal_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def read_journal(journal_path: str = DEFAULT_JOURNAL_PATH, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """Relit le journal (utile pour calculer le résumé journalier ou auditer)."""
    if not os.path.exists(journal_path):
        return []
    with open(journal_path, "r", encoding="utf-8") as f:
        lines = [json.loads(line) for line in f if line.strip()]
    if limit:
        return lines[-limit:]
    return lines


# --- Métadonnées de position (zone_reference_price, partial_exit_taken) ---
# OANDA ne stocke pas ces informations stratégiques : on les garde localement,
# indexées par trade_id OANDA.

def save_position_metadata(trade_id: str, metadata: Dict[str, Any], path: str = DEFAULT_POSITIONS_META_PATH) -> None:
    _ensure_dir(path)
    with _LOCK:
        data = _load_metadata_file(path)
        data[trade_id] = metadata
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)


def get_position_metadata(trade_id: str, path: str = DEFAULT_POSITIONS_META_PATH) -> Optional[Dict[str, Any]]:
    data = _load_metadata_file(path)
    return data.get(trade_id)


def delete_position_metadata(trade_id: str, path: str = DEFAULT_POSITIONS_META_PATH) -> None:
    with _LOCK:
        data = _load_metadata_file(path)
        if trade_id in data:
            del data[trade_id]
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)


def _load_metadata_file(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        content = f.read().strip()
        return json.loads(content) if content else {}


# --- Compteurs journaliers (perte cumulée, trades perdants) ---

DEFAULT_DAILY_STATE_PATH = os.environ.get("DAILY_STATE_PATH", "/home/claude/trading_engine/data/daily_state.json")


def get_daily_state(date_str: str, path: str = DEFAULT_DAILY_STATE_PATH) -> Dict[str, Any]:
    """
    Retourne l'état journalier {"perte_du_jour_cumulee": float, "nombre_trades_perdants_jour": int}
    pour la date donnée (format "YYYY-MM-DD"). Réinitialisé automatiquement à un nouveau jour.
    """
    all_state = _load_metadata_file(path)
    return all_state.get(date_str, {"perte_du_jour_cumulee": 0.0, "nombre_trades_perdants_jour": 0})


def update_daily_state(date_str: str, loss_delta: float, is_losing_trade: bool, path: str = DEFAULT_DAILY_STATE_PATH) -> Dict[str, Any]:
    """Met à jour l'état journalier après la clôture d'un trade."""
    with _LOCK:
        all_state = _load_metadata_file(path)
        current = all_state.get(date_str, {"perte_du_jour_cumulee": 0.0, "nombre_trades_perdants_jour": 0})
        current["perte_du_jour_cumulee"] = round(current["perte_du_jour_cumulee"] + max(loss_delta, 0.0), 2)
        if is_losing_trade:
            current["nombre_trades_perdants_jour"] += 1
        all_state[date_str] = current
        _ensure_dir(path)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(all_state, f, ensure_ascii=False, indent=2)
        return current
