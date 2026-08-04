"""
api.py

API HTTP exposant le moteur de décision (decision_engine.analyze) au serveur
externe qui gère la collecte de données de marché et l'exécution des ordres.

Endpoint principal: POST /analyze
- Reçoit le payload JSON conforme à la section 10 de la spec
- Retourne la décision structurée (HOLD | ENTER | EXIT | REDUCE)

Endpoint santé: GET /health

Lancement:
    uvicorn trading_engine.api:app --host 0.0.0.0 --port 8000
"""

import logging
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, ConfigDict

from .decision_engine import analyze
from . import risk_manager as rm
from . import telegram_notifier as tg

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("trading_engine.api")

app = FastAPI(
    title="Moteur de décision — XAUUSD Trading Engine",
    description="Reçoit l'état du marché, retourne une décision de trading structurée.",
    version="1.0.0",
)


# --- Schémas Pydantic (validation stricte du format d'entrée, section 10) ---

class Candle(BaseModel):
    open: float
    high: float
    low: float
    close: float


class CompteInfo(BaseModel):
    solde: float
    equite: float
    marge_utilisee: float
    marge_disponible: float
    positions_ouvertes: List[Dict[str, Any]] = Field(default_factory=list)


class PrixInfo(BaseModel):
    actuel: float
    bid: Optional[float] = None
    ask: Optional[float] = None
    spread: Optional[float] = None


class TimeframeData(BaseModel):
    model_config = ConfigDict(extra="allow")
    ohlc: List[Candle]
    ema50: Optional[float] = None
    ema200: Optional[float] = None
    atr14: Optional[float] = None
    atr14_moy20: Optional[float] = None
    bollinger: Optional[Dict[str, float]] = None
    rsi14: Optional[float] = None
    volume: Optional[List[float]] = None


class MacroEvent(BaseModel):
    nom: Optional[str] = None
    impact: str  # "high" | "medium" | "low"
    minutes_avant: float


class AnalyzeRequest(BaseModel):
    timestamp: str
    compte: CompteInfo
    prix: PrixInfo
    D1: Optional[TimeframeData] = None  # optionnel — version août utilise H4/H1/M30
    H4: TimeframeData
    H1: TimeframeData
    M30: Optional[TimeframeData] = None  # AJOUTÉ pour version août
    M15: TimeframeData
    M5: TimeframeData
    evenements_macro_a_venir: List[MacroEvent] = Field(default_factory=list)
    declencheur_alerte: str = "cyclique_30min"
    perte_du_jour_cumulee: float = 0.0
    nombre_trades_perdants_jour: int = 0


class AnalyzeResponse(BaseModel):
    decision: str
    direction: Optional[str] = None
    entry: Optional[float] = None
    sl: Optional[float] = None
    tp: Optional[float] = None
    risque_dollars: Optional[float] = None
    rr_vise: Optional[float] = None
    scd: Optional[int] = None
    irv: Optional[float] = None
    fqe_score: Optional[int] = None
    raisonnement: str
    zone_reference_price: Optional[float] = None
    nouveau_sl: Optional[float] = None
    pourcentage_reduction: Optional[float] = None
    timestamp_analyse: str


@app.get("/health")
def health() -> Dict[str, str]:
    """Vérification de santé du service."""
    return {"status": "ok", "service": "xauusd-decision-engine"}


@app.get("/risk-parameters")
def risk_parameters() -> Dict[str, Any]:
    """Expose les paramètres de risque figés (utile pour audit côté serveur)."""
    return {
        "risque_min_par_trade": rm.RISK_PER_TRADE_MIN_DOLLARS,
        "perte_max_journaliere": rm.DAILY_LOSS_MAX_DOLLARS,
        "seuil_alerte_perte_journaliere": rm.DAILY_LOSS_ALERT_THRESHOLD_DOLLARS,
        "positions_max_simultanees": rm.MAX_OPEN_POSITIONS,
        "rr_minimum": rm.MIN_RISK_REWARD,
        "trades_perdants_max_par_jour": rm.MAX_LOSING_TRADES_PER_DAY,
    }


@app.post("/analyze", response_model=AnalyzeResponse)
def analyze_endpoint(payload: AnalyzeRequest) -> AnalyzeResponse:
    """
    Endpoint principal. Convertit le payload validé en dict, appelle le moteur
    de décision, retourne la décision structurée, et notifie Telegram en tâche
    non bloquante (un échec Telegram ne fait jamais échouer la décision).
    """
    try:
        payload_dict = payload.model_dump()
        result = analyze(payload_dict)
        result["timestamp_analyse"] = datetime.now(timezone.utc).isoformat()
        logger.info(
            "Analyse traitée: decision=%s scd=%s irv=%s",
            result.get("decision"), result.get("scd"), result.get("irv"),
        )

        try:
            tg.notify_decision(result)
        except Exception:
            logger.exception("Notification Telegram non envoyée (erreur absorbée)")

        return AnalyzeResponse(**result)
    except Exception as exc:
        logger.exception("Erreur pendant l'analyse")
        raise HTTPException(status_code=500, detail=f"Erreur d'analyse: {exc}") from exc


class UrgentAlertRequest(BaseModel):
    reason: str


class DailySummaryRequest(BaseModel):
    date: str
    nb_trades: int
    nb_gagnants: int
    nb_perdants: int
    pnl_du_jour: float
    solde_actuel: float


@app.post("/notify/urgent-alert")
def notify_urgent_alert_endpoint(payload: UrgentAlertRequest) -> Dict[str, Any]:
    """
    Endpoint dédié aux interventions immédiates (section 11) — le serveur
    l'appelle directement pour les cas hors cycle normal (spread anormal,
    mouvement défavorable à 50% du SL, gap d'ouverture, etc.).
    """
    sent = tg.notify_urgent_alert(payload.reason)
    return {"sent": sent}


@app.post("/notify/daily-summary")
def notify_daily_summary_endpoint(payload: DailySummaryRequest) -> Dict[str, Any]:
    """Endpoint pour le résumé journalier (section 12 — journalisation)."""
    sent = tg.notify_daily_summary(payload.model_dump())
    return {"sent": sent}
