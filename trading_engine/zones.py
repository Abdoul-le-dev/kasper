"""
zones.py

Détection des zones d'intérêt (support / résistance / liquidité) à partir
des pivots de prix sur D1/H4, avec clustering des niveaux proches.
"""

from typing import List, Dict, Optional


def find_pivots(ohlc: List[Dict], left: int = 3, right: int = 3) -> Dict[str, List[float]]:
    """
    Identifie les pivots hauts et bas (swing highs / swing lows) d'une série OHLC.
    Un pivot haut est un `high` supérieur à `left` bougies avant et `right` bougies après.
    """
    highs = [c["high"] for c in ohlc]
    lows = [c["low"] for c in ohlc]
    n = len(ohlc)

    pivot_highs = []
    pivot_lows = []

    for i in range(left, n - right):
        window_h = highs[i - left:i + right + 1]
        window_l = lows[i - left:i + right + 1]
        if highs[i] == max(window_h):
            pivot_highs.append(highs[i])
        if lows[i] == min(window_l):
            pivot_lows.append(lows[i])

    return {"highs": pivot_highs, "lows": pivot_lows}


def cluster_levels(levels: List[float], tolerance_pct: float = 0.0015) -> List[Dict]:
    """
    Regroupe les niveaux de prix proches (dans une tolérance relative) en zones,
    et compte le nombre de "touches" comme proxy de force de la zone.

    Retourne une liste de dicts {"price": float, "touches": int}, triée par
    nombre de touches décroissant.
    """
    if not levels:
        return []

    sorted_levels = sorted(levels)
    clusters = []
    current_cluster = [sorted_levels[0]]

    for lvl in sorted_levels[1:]:
        ref = current_cluster[-1]
        if abs(lvl - ref) / ref <= tolerance_pct:
            current_cluster.append(lvl)
        else:
            clusters.append(current_cluster)
            current_cluster = [lvl]
    clusters.append(current_cluster)

    zones = [
        {"price": sum(c) / len(c), "touches": len(c)}
        for c in clusters
    ]
    zones.sort(key=lambda z: z["touches"], reverse=True)
    return zones


def identify_zones(
    ohlc_d1: List[Dict],
    ohlc_h4: List[Dict],
    max_zones: int = 4,
    tolerance_pct: float = 0.0015,
) -> List[Dict]:
    """
    Identifie jusqu'à `max_zones` zones d'intérêt (support/résistance) en combinant
    les pivots D1 (poids structurel fort) et H4 (poids structurel modéré).

    Retourne une liste de dicts: {"price": float, "type": "resistance"|"support", "touches": int}
    """
    pivots_d1 = find_pivots(ohlc_d1, left=2, right=2)
    pivots_h4 = find_pivots(ohlc_h4, left=3, right=3)

    all_highs = pivots_d1["highs"] + pivots_h4["highs"]
    all_lows = pivots_d1["lows"] + pivots_h4["lows"]

    resistance_zones = cluster_levels(all_highs, tolerance_pct)
    support_zones = cluster_levels(all_lows, tolerance_pct)

    zones = []
    for z in resistance_zones[:max_zones]:
        zones.append({"price": z["price"], "type": "resistance", "touches": z["touches"]})
    for z in support_zones[:max_zones]:
        zones.append({"price": z["price"], "type": "support", "touches": z["touches"]})

    zones.sort(key=lambda z: z["touches"], reverse=True)
    return zones[:max_zones]


def nearest_zone(price: float, zones: List[Dict]) -> Optional[Dict]:
    """Retourne la zone la plus proche du prix actuel (ou None si aucune zone)."""
    if not zones:
        return None
    return min(zones, key=lambda z: abs(z["price"] - price))


def zone_proximity_type(price: float, zones: List[Dict], atr_value: float, max_distance_factor: float = 0.3) -> Optional[str]:
    """
    Détermine si le prix est suffisamment proche d'une zone pour être considéré
    "en zone de demande" (support) ou "en zone d'offre" (résistance).

    Distance acceptable: max_distance_factor * atr_value.
    Retourne "demand" | "supply" | None.
    """
    zone = nearest_zone(price, zones)
    if zone is None:
        return None
    max_distance = max_distance_factor * atr_value
    if abs(zone["price"] - price) > max_distance:
        return None
    return "demand" if zone["type"] == "support" else "supply"