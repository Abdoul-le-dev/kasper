"""
session_context.py

Détermine la session forex/or actuelle et la cadence de trading associée.

Sessions (horaires UTC, standard sans DST):
- Asie (Tokyo)     : 00:00 - 09:00
- Londres          : 07:00 - 16:00
- New York (US)    : 13:00 - 22:00
- Overlap London/NY: 13:00 - 16:00 (le plus liquide sur l'or)

Cadence:
- Session US ouverte (13:00-22:00 UTC lun-ven) → cycle 5 min
- Hors session US → cycle 25 min
- Weekend (samedi/dimanche) → cycle 30 min et Claude renverra probablement HOLD
"""

from datetime import datetime, timezone
from typing import Dict, Optional


ASIA_START = 0
ASIA_END = 9
LONDON_START = 7
LONDON_END = 16
NY_START = 13
NY_END = 22

CYCLE_MINUTES_US_SESSION = 5
CYCLE_MINUTES_OFF_SESSION = 25
CYCLE_MINUTES_WEEKEND = 30


def get_session_info(now: Optional[datetime] = None) -> Dict:
    """
    Retourne les infos de session à l'instant `now` (UTC).
    """
    if now is None:
        now = datetime.now(timezone.utc)

    hour = now.hour
    weekday = now.weekday()  # 0=lundi, 6=dimanche
    is_weekend = weekday >= 5  # samedi (5) ou dimanche (6)

    asia_open = ASIA_START <= hour < ASIA_END
    london_open = LONDON_START <= hour < LONDON_END
    ny_open = NY_START <= hour < NY_END
    overlap_london_ny = london_open and ny_open

    if is_weekend:
        session_label = "weekend"
    elif overlap_london_ny:
        session_label = "overlap_london_ny"
    elif ny_open:
        session_label = "new_york"
    elif london_open:
        session_label = "london"
    elif asia_open:
        session_label = "asia"
    else:
        session_label = "quiet"  # 22h-00h UTC, marché très calme

    # Cadence
    if is_weekend:
        cycle_minutes = CYCLE_MINUTES_WEEKEND
    elif ny_open:
        cycle_minutes = CYCLE_MINUTES_US_SESSION
    else:
        cycle_minutes = CYCLE_MINUTES_OFF_SESSION

    # Temps restant dans la session la plus active
    if ny_open:
        minutes_to_close = (NY_END - hour) * 60 - now.minute
    elif london_open:
        minutes_to_close = (LONDON_END - hour) * 60 - now.minute
    elif asia_open:
        minutes_to_close = (ASIA_END - hour) * 60 - now.minute
    else:
        minutes_to_close = None

    return {
        "session_label": session_label,
        "asia_open": asia_open,
        "london_open": london_open,
        "ny_open": ny_open,
        "overlap_london_ny": overlap_london_ny,
        "is_weekend": is_weekend,
        "cycle_minutes": cycle_minutes,
        "minutes_to_session_close": minutes_to_close,
        "utc_hour": hour,
        "utc_weekday": weekday,
    }
