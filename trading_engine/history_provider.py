"""
history_provider.py

Fournit à Claude un résumé synthétique de ses N dernières décisions, pour
lui éviter les contradictions, l'over-trading et les indécisions répétées.

Lit le journal JSONL existant (journal.py) et extrait les métadonnées utiles.
"""

from typing import Dict, List

from . import journal


def get_recent_decisions_summary(n: int = 10) -> List[Dict]:
    """
    Retourne les `n` dernières décisions extraites du journal, dans l'ordre
    chronologique (plus ancienne d'abord).

    Format retourné (concis pour ne pas gonfler le payload):
    [
        {
            "timestamp": "2026-08-03T20:30:00Z",
            "decision": "HOLD",
            "direction": null,
            "raisonnement_court": "premiers 200 caractères du raisonnement",
            "confiance": "moyenne",
            "prix_actuel_au_moment": 2000.0,
        },
        ...
    ]
    """
    entries = journal.read_journal(limit=n)
    if not entries:
        return []

    summary = []
    for entry in entries:
        decision = entry.get("decision", {})
        input_summary = entry.get("input_summary", {})
        raisonnement = decision.get("raisonnement", "") or ""
        summary.append({
            "timestamp": entry.get("logged_at"),
            "decision": decision.get("decision"),
            "direction": decision.get("direction"),
            "raisonnement_court": raisonnement[:200] + ("…" if len(raisonnement) > 200 else ""),
            "confiance": decision.get("confiance"),
            "prix_actuel_au_moment": input_summary.get("prix_actuel"),
            "rr_vise": decision.get("rr_vise"),
        })

    return summary


def count_recent_decisions_by_type(n: int = 10) -> Dict[str, int]:
    """
    Compte les types de décisions récentes — utile pour détecter des patterns
    (ex: 8 HOLD sur 10, ou 3 ENTER rapprochés).
    """
    recent = get_recent_decisions_summary(n=n)
    counts = {"HOLD": 0, "ENTER": 0, "EXIT": 0, "REDUCE": 0}
    for d in recent:
        decision_type = d.get("decision")
        if decision_type in counts:
            counts[decision_type] += 1
    return counts
