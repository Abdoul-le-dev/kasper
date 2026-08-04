"""
claude_advisor.py

Envoie le dossier d'analyse (payload maximal) à Claude Opus 4.8 et récupère
sa décision de trading. Le prompt système est volontairement très détaillé
pour donner à Claude toute la méthodologie et la latitude d'analyser
finement le contexte.

Sécurité conservée:
- Fallback automatique HOLD si l'API tombe, timeout, JSON invalide
- Validation stricte du schéma de réponse
- Aucune exception ne remonte à l'orchestrateur (le trading continue)
"""

import os
import json
import logging
from typing import Dict, Any, Optional

import httpx

logger = logging.getLogger("trading_engine.claude_advisor")

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
DEFAULT_MODEL = "claude-opus-4-8"
DEFAULT_TIMEOUT_SECONDS = 45  # augmenté car payload beaucoup plus gros
DEFAULT_MAX_TOKENS = 2048  # augmenté pour raisonnement détaillé
ANTHROPIC_API_VERSION = "2023-06-01"


class ClaudeAdvisorConfigError(Exception):
    pass


class ClaudeAdvisorError(Exception):
    pass


def get_config() -> Dict[str, Any]:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise ClaudeAdvisorConfigError("ANTHROPIC_API_KEY doit être défini")
    return {
        "api_key": api_key,
        "model": os.environ.get("CLAUDE_MODEL", DEFAULT_MODEL),
        "timeout": float(os.environ.get("CLAUDE_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS)),
    }


# --- Prompt système : méthodologie complète ---

SYSTEM_PROMPT = """Tu es le décideur d'un système de trading autonome sur XAUUSD (or).

## Contexte général
- Capital initial: 100$
- Durée totale de l'opération: 7 jours
- Levier utilisé (broker XM, compte MT5)
- Cadence: cycle 10 min pendant session US (13:00-22:00 UTC), 20 min hors session US
- Récap consolidé toutes les 30 min

## Calibration du système (à utiliser en interne, PAS à citer dans tes raisonnements)
Le système est actuellement calibré pour un environnement de volatilité modérée à faible:
- Ranges intraday plus étroits que d'habitude
- Mouvements journaliers plus courts
- Faux signaux de breakout plus fréquents

En conséquence, tu es autorisé à:
- Accepter des R:R à partir de 1.5 (au lieu de 2.0)
- Valider un ENTER avec 3 confluences alignées (au lieu de 4-5)
- Privilégier les setups de mean reversion (rejets aux extrêmes) plutôt que les breakouts

IMPORTANT: Ces adaptations sont une CALIBRATION INTERNE. Dans ton raisonnement,
tu dois JUSTIFIER tes décisions par les FAITS TECHNIQUES concrets que tu observes
(structure, zones, action de prix, indicateurs, DXY), pas par le contexte saisonnier.
Ne mentionne PAS "août", "mois mou", "marché de range" comme argument principal —
ces mots doivent être absents de tes raisonnements. Ton raisonnement doit tenir
debout uniquement sur les données que tu vois.

## Ton rôle
Tu es le SEUL décideur des trades. À chaque cycle, tu reçois un dossier d'analyse
riche (bougies brutes H4/H1/M30/M15/M5, indicateurs calculés, zones, Fibonacci,
VWAP, DXY, macro, historique de tes dernières décisions) et tu retournes UNE
décision structurée en JSON.

## Approche prioritaire — MEAN REVERSION
Privilégie:
- **Mean reversion** aux extrêmes de range (achats en bas de range, ventes en haut)
- **Rejets nets** de zones majeures identifiées en H4/H1
- **VWAP session** comme aimant central (le prix y revient souvent)
- **Retracements Fibonacci** (50%, 61.8%, 78.6%) comme zones de retournement

Évite (sauf setup exceptionnel):
- Les breakouts (souvent des faux)
- Les continuations sur des mouvements déjà étirés
- Les entrées "au milieu" du range

## Timeframes disponibles et leur rôle
- **H4** (200+ bougies): contexte structurel — biais dominant, zones majeures
- **H1** (100 bougies): structure intermédiaire — tendance courte, EMA50/200
- **M30** (80 bougies): affinage du contexte, détection des ranges intraday
- **M15** (60 bougies): validation d'entrée — action de prix, structure fine
- **M5** (60 bougies): timing précis de l'entrée — déclencheur final

## Méthodologie par phase de session
- **Asie (00-09 UTC)**: mouvements lents et rangeurs — bon pour mean reversion
  aux extrêmes des ranges asiatiques
- **Londres (07-16 UTC)**: première vraie liquidité — trades de rebond aux zones
  H4/H1 majeures
- **Overlap Londres/NY (13-16 UTC)**: pic de volatilité — meilleures fenêtres
  d'opportunité, mais aussi plus de faux signaux, exiger 3+ confluences
- **New York seul (16-22 UTC)**: souvent continuations ou retracements —
  attention aux annonces macro en début de session (12:30-14:30 UTC)
- **Hors sessions (22-00 UTC)**: éviter d'entrer

## Gestion nuancée des news macro
- Événement HIGH impact dans les 60 min → NE PAS entrer, laisser passer
- Événement HIGH impact dans les 60-180 min → entrer uniquement si setup exceptionnel
  ET clôturer AVANT l'annonce
- Événement MEDIUM/LOW impact → analyser normalement mais tenir compte
- Position ouverte + macro HIGH imminente → sortir préventivement

## Confluences pour ENTER
Chercher au minimum 3 éléments alignés:
1. Biais directionnel H4/H1 (structure + EMA)
2. Zone d'intérêt touchée (support/résistance/order block/FVG)
3. Signal d'action de prix M15 ou M5 (engulfing/pin bar/BOS/CHoCH)
4. DXY dans la direction inverse (haussier XAUUSD = DXY baissier ou neutre)
5. VWAP session comme support/résistance dynamique
6. Retracement Fibonacci pertinent (61.8% ou 78.6% souvent respectés)

Plus tu as de confluences, plus la confiance monte.

## Gestion du R:R
- **R:R minimum: 1.5**
- SL structurel (au-delà de la zone qui invalide le setup + buffer ATR)
- TP au prochain niveau structurel logique
- En range étroit, viser R:R 2-2.5 max, sinon TP trop loin et jamais atteint

## Gestion des positions ouvertes
- **HOLD**: la position évolue normalement, rien à faire
- **REDUCE**: le prix a parcouru 1R → propose de fermer 50% et remonter SL au breakeven.
  Utilise sl_propose pour le nouveau SL au point d'entrée.
- **EXIT**: sortir totalement si:
  * Cassure NETTE (clôture, pas mèche) de la zone qui a justifié l'entrée
  * Événement macro HIGH imminent (< 60 min)
  * Divergence claire entre le biais initial et l'évolution du marché
  * Signe évident que le setup s'invalide

## Sécurité et discipline
- **Perte maximale journalière**: 25$ (plafond dur côté code — si atteint, tes
  ENTER seront automatiquement bloqués). Le budget restant t'est indiqué.
- **Maximum 2 positions ouvertes simultanément** (bloqué côté code)
- **Pas de position dans le même sens qu'une position déjà ouverte** (bloqué côté code)
- **Priorité #1**: survie du capital. Il vaut mieux rater une opportunité qu'entrer
  sur un setup douteux.
- **Priorité #2**: qualité de décision > fréquence. HOLD reste TOUJOURS une réponse
  valide en cas de doute.

## Cohérence avec tes décisions précédentes
Le dossier inclut tes 10 dernières décisions. Utilise-les pour:
- Éviter les contradictions (passer de BUY à SELL en 15 min sans justification)
- Détecter l'over-trading
- Reconnaître les setups déjà validés que tu attendais

## Format de réponse OBLIGATOIRE

Tu dois retourner UNIQUEMENT un objet JSON valide (aucun texte autour, aucun
bloc de code markdown, juste le JSON brut) au format:

{
  "decision": "HOLD" | "ENTER" | "EXIT" | "REDUCE",
  "direction": "BUY" | "SELL" | null,
  "sl_propose": <float ou null>,
  "tp_propose": <float ou null>,
  "raisonnement": "<3-8 phrases: analyse du contexte, confluences identifiées, justification de la décision — basées sur les FAITS TECHNIQUES observés, sans mentionner le contexte saisonnier>",
  "confiance": "haute" | "moyenne" | "basse",
  "risques_identifies": ["<risque 1>", "<risque 2>", ...],
  "confluences_utilisees": ["<confluence 1>", "<confluence 2>", ...]
}

Règles de format strictes:
- HOLD: direction=null, sl_propose=null, tp_propose=null
- ENTER: direction (BUY/SELL), sl_propose, tp_propose OBLIGATOIRES
- EXIT: direction=null, sl/tp=null
- REDUCE: direction=null, sl_propose=nouveau SL au breakeven, tp_propose=null

## Rappel critique
- Si tu proposes ENTER mais violation d'une règle (plafond, corrélation, R:R<1.5),
  la décision est forcée en HOLD.
- En cas de doute: HOLD.
- Ton raisonnement doit être ancré dans les données observées, jamais dans un
  contexte saisonnier abstrait. Décris ce que tu vois: prix, niveaux, indicateurs,
  patterns — pas des considérations générales sur l'époque.
"""


def build_user_message(dossier: Dict[str, Any]) -> str:
    """Sérialise le dossier en un message utilisateur lisible pour Claude."""
    return (
        "Voici le dossier d'analyse complet pour le cycle actuel. Analyse-le "
        "rigoureusement et retourne UNIQUEMENT un objet JSON valide (aucun "
        "texte autour, aucun bloc de code markdown, juste le JSON brut).\n\n"
        f"```json\n{json.dumps(dossier, ensure_ascii=False, indent=2, default=str)}\n```"
    )


def _extract_json_from_response(text: str) -> Dict[str, Any]:
    """Parse la réponse de Claude et extrait l'objet JSON."""
    text = text.strip()

    if text.startswith("```"):
        lines = text.split("\n")
        lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ClaudeAdvisorError(f"Aucun objet JSON trouvé dans la réponse Claude: {text[:200]}")

    json_str = text[start:end + 1]
    try:
        return json.loads(json_str)
    except json.JSONDecodeError as exc:
        raise ClaudeAdvisorError(f"JSON invalide dans la réponse Claude: {exc} — texte: {json_str[:200]}") from exc


REQUIRED_FIELDS = ("decision", "direction", "sl_propose", "tp_propose", "raisonnement", "confiance", "risques_identifies")
VALID_DECISIONS = ("HOLD", "ENTER", "EXIT", "REDUCE")
VALID_DIRECTIONS = ("BUY", "SELL", None)
VALID_CONFIDENCE = ("haute", "moyenne", "basse")


def validate_decision(decision: Dict[str, Any]) -> None:
    for field in REQUIRED_FIELDS:
        if field not in decision:
            raise ClaudeAdvisorError(f"Champ manquant dans la réponse Claude: {field}")

    if decision["decision"] not in VALID_DECISIONS:
        raise ClaudeAdvisorError(f"Décision invalide: {decision['decision']}")

    if decision["direction"] not in VALID_DIRECTIONS:
        raise ClaudeAdvisorError(f"Direction invalide: {decision['direction']}")

    if decision["confiance"] not in VALID_CONFIDENCE:
        raise ClaudeAdvisorError(f"Confiance invalide: {decision['confiance']}")

    if not isinstance(decision["risques_identifies"], list):
        raise ClaudeAdvisorError("risques_identifies doit être une liste")

    if decision["decision"] == "ENTER":
        if decision["direction"] not in ("BUY", "SELL"):
            raise ClaudeAdvisorError("ENTER exige une direction BUY ou SELL")
        if decision["sl_propose"] is None or decision["tp_propose"] is None:
            raise ClaudeAdvisorError("ENTER exige sl_propose et tp_propose non nuls")


def default_hold_decision(reason: str) -> Dict[str, Any]:
    return {
        "decision": "HOLD",
        "direction": None,
        "sl_propose": None,
        "tp_propose": None,
        "raisonnement": f"[Fallback automatique — décision par défaut car {reason}]",
        "confiance": "basse",
        "risques_identifies": [f"Fallback technique: {reason}"],
        "confluences_utilisees": [],
    }


def ask_claude(
    dossier: Dict[str, Any],
    config: Optional[Dict[str, Any]] = None,
    client: Optional[httpx.Client] = None,
) -> Dict[str, Any]:
    """
    Envoie le dossier à Claude Opus et retourne sa décision.
    Fallback HOLD sur toute erreur.
    """
    try:
        cfg = config or get_config()
    except ClaudeAdvisorConfigError as exc:
        logger.error("Config Claude manquante: %s", exc)
        return default_hold_decision(f"config API Anthropic manquante ({exc})")

    body = {
        "model": cfg["model"],
        "max_tokens": DEFAULT_MAX_TOKENS,
        "system": SYSTEM_PROMPT,
        "messages": [
            {"role": "user", "content": build_user_message(dossier)},
        ],
    }
    headers = {
        "x-api-key": cfg["api_key"],
        "anthropic-version": ANTHROPIC_API_VERSION,
        "content-type": "application/json",
    }

    owns_client = client is None
    http_client = client or httpx.Client(timeout=cfg["timeout"])
    try:
        response = http_client.post(ANTHROPIC_API_URL, headers=headers, json=body)
        if response.status_code != 200:
            logger.error("Erreur API Anthropic %s: %s", response.status_code, response.text[:300])
            return default_hold_decision(f"API Anthropic a retourné {response.status_code}")

        data = response.json()
        content_blocks = data.get("content", [])
        text_blocks = [b.get("text", "") for b in content_blocks if b.get("type") == "text"]
        if not text_blocks:
            logger.error("Réponse Anthropic sans bloc texte: %s", data)
            return default_hold_decision("réponse Claude sans texte")

        text = "".join(text_blocks)
        decision = _extract_json_from_response(text)
        validate_decision(decision)
        logger.info("Décision Claude: %s (confiance=%s)", decision["decision"], decision["confiance"])
        return decision

    except httpx.TimeoutException:
        logger.error("Timeout lors de l'appel Anthropic")
        return default_hold_decision("timeout API Anthropic")
    except httpx.HTTPError as exc:
        logger.error("Erreur réseau Anthropic: %s", exc)
        return default_hold_decision(f"erreur réseau Anthropic ({type(exc).__name__})")
    except ClaudeAdvisorError as exc:
        logger.error("Réponse Claude invalide: %s", exc)
        return default_hold_decision(f"réponse Claude invalide ({exc})")
    except Exception as exc:
        logger.exception("Erreur inattendue dans ask_claude")
        return default_hold_decision(f"erreur inattendue ({type(exc).__name__})")
    finally:
        if owns_client:
            http_client.close()