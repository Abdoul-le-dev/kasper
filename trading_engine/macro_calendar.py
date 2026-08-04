"""
macro_calendar.py

Récupère les événements macro à venir (USD principalement, car ils impactent
directement l'or) avec bascule automatique entre deux sources gratuites :
1. TradingEconomics API guest (limité mais suffisant)
2. ForexFactory JSON public (fallback)

Si les deux sources tombent, retourne une liste vide avec une note.
"""

import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional

import httpx

logger = logging.getLogger("trading_engine.macro_calendar")

FOREXFACTORY_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
TRADINGECONOMICS_URL = "https://api.tradingeconomics.com/calendar"

DEFAULT_TIMEOUT = 10.0
HIGH_IMPACT_HORIZON_HOURS = 24  # regarder 24h à l'avance


class MacroCalendarError(Exception):
    pass


def get_upcoming_events(
    hours_ahead: int = HIGH_IMPACT_HORIZON_HOURS,
    client: Optional[httpx.Client] = None,
) -> List[Dict]:
    """
    Retourne les événements macro USD à impact HIGH prévus dans les
    `hours_ahead` prochaines heures.

    Format des événements retournés (compatible avec le format déjà utilisé
    par le moteur):
        {"nom": str, "impact": "high", "minutes_avant": int, "monnaie": "USD"}
    """
    now = datetime.now(timezone.utc)
    events = _try_forexfactory(now, hours_ahead, client)
    if events is not None:
        return events

    events = _try_tradingeconomics(now, hours_ahead, client)
    if events is not None:
        return events

    logger.warning("Aucune source macro disponible ce cycle")
    return []


def _try_forexfactory(
    now: datetime, hours_ahead: int, client: Optional[httpx.Client]
) -> Optional[List[Dict]]:
    """
    ForexFactory JSON public (mis à jour hebdo).
    Format brut: liste d'objets avec {title, country, date, impact, forecast, previous}
    """
    owns_client = client is None
    http_client = client or httpx.Client(
        timeout=DEFAULT_TIMEOUT,
        headers={"User-Agent": "Mozilla/5.0 (trading-engine)"},
    )
    try:
        response = http_client.get(FOREXFACTORY_URL)
        if response.status_code != 200:
            logger.info("ForexFactory HTTP %s", response.status_code)
            return None

        raw_events = response.json()
        return _filter_events_forexfactory(raw_events, now, hours_ahead)
    except (httpx.HTTPError, ValueError) as exc:
        logger.info("ForexFactory indisponible: %s", exc)
        return None
    finally:
        if owns_client:
            http_client.close()


def _filter_events_forexfactory(
    raw_events: List[Dict], now: datetime, hours_ahead: int
) -> List[Dict]:
    """Filtre les événements USD à impact HIGH dans la fenêtre temporelle."""
    horizon = now + timedelta(hours=hours_ahead)
    result = []

    for e in raw_events:
        country = e.get("country", "").upper()
        impact = e.get("impact", "").lower()
        if country != "USD" or impact != "high":
            continue

        # Parser la date ISO
        date_str = e.get("date")
        if not date_str:
            continue
        try:
            event_time = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            if event_time.tzinfo is None:
                event_time = event_time.replace(tzinfo=timezone.utc)
        except ValueError:
            continue

        if event_time <= now or event_time > horizon:
            continue

        minutes_avant = int((event_time - now).total_seconds() / 60)
        result.append({
            "nom": e.get("title", "Unknown"),
            "impact": "high",
            "monnaie": "USD",
            "minutes_avant": minutes_avant,
            "heure_utc": event_time.isoformat(),
            "forecast": e.get("forecast"),
            "previous": e.get("previous"),
        })

    result.sort(key=lambda x: x["minutes_avant"])
    return result


def _try_tradingeconomics(
    now: datetime, hours_ahead: int, client: Optional[httpx.Client]
) -> Optional[List[Dict]]:
    """
    TradingEconomics API guest (accès limité mais gratuit sans clé).
    Endpoint guest limité à 1 an de données et faible fréquence.
    """
    owns_client = client is None
    http_client = client or httpx.Client(
        timeout=DEFAULT_TIMEOUT,
        headers={"User-Agent": "Mozilla/5.0 (trading-engine)"},
    )
    try:
        params = {
            "c": "guest:guest",
            "country": "united states",
            "importance": "3",  # 3 = high impact
            "format": "json",
        }
        response = http_client.get(TRADINGECONOMICS_URL, params=params)
        if response.status_code != 200:
            logger.info("TradingEconomics HTTP %s", response.status_code)
            return None

        raw_events = response.json()
        return _filter_events_tradingeconomics(raw_events, now, hours_ahead)
    except (httpx.HTTPError, ValueError) as exc:
        logger.info("TradingEconomics indisponible: %s", exc)
        return None
    finally:
        if owns_client:
            http_client.close()


def _filter_events_tradingeconomics(
    raw_events: List[Dict], now: datetime, hours_ahead: int
) -> List[Dict]:
    horizon = now + timedelta(hours=hours_ahead)
    result = []

    for e in raw_events:
        date_str = e.get("Date")
        if not date_str:
            continue
        try:
            event_time = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            if event_time.tzinfo is None:
                event_time = event_time.replace(tzinfo=timezone.utc)
        except ValueError:
            continue

        if event_time <= now or event_time > horizon:
            continue

        minutes_avant = int((event_time - now).total_seconds() / 60)
        result.append({
            "nom": e.get("Event", "Unknown"),
            "impact": "high",
            "monnaie": "USD",
            "minutes_avant": minutes_avant,
            "heure_utc": event_time.isoformat(),
            "forecast": e.get("Forecast"),
            "previous": e.get("Previous"),
        })

    result.sort(key=lambda x: x["minutes_avant"])
    return result
