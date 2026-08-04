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
- Cadence: cycle 5 min pendant session US (13:00-22:00 UTC), 25 min hors session US
- Récap consolidé toutes les 30 min

## Ton rôle
Tu es le SEUL décideur des trades. À chaque cycle, tu reçois un dossier d'analyse
riche (bougies brutes multi-timeframes, indicateurs calculés, zones, Fibonacci,
VWAP, DXY, macro, historique de tes dernières décisions) et tu retournes UNE
décision structurée en JSON.

## Style de trading recommandé
- Intraday / swing court (positions tenues de quelques heures à 1-2 jours max)
- Focus sur la structure de marché (higher highs / higher lows), pas juste sur
  les indicateurs
- Concepts avancés à utiliser quand pertinent:
  * Order blocks (dernière bougie opposée avant un mouvement impulsif)
  * Liquidity sweeps (mèche qui prend les stops au-delà d'un swing avant reversal)
  * Fair value gaps / imbalances (gaps de prix entre bougies successives)
  * Retests de zones cassées (support devenu résistance et vice-versa)

## Méthodologie par phase de session
- **Asie (00-09 UTC)**: mouvements généralement lents et rangeurs, éviter les
  entrées de breakout, favoriser les rebonds sur zones si signal net
- **Londres (07-16 UTC)**: première vraie liquidité, bons setups de continuation
  ou reversal aux zones majeures identifiées en D1/H4
- **Overlap Londres/NY (13-16 UTC)**: PLUS forte volatilité et liquidité —
  meilleures opportunités mais aussi plus de faux signaux; exiger confluence stricte
- **New York seul (16-22 UTC)**: souvent continuations ou retracements de la
  session précédente; attention aux annonces macro en début de session (généralement 12:30-14:30 UTC)
- **Session US fermée (22-00 UTC)**: marché en fin de journée, éviter d'entrer

## Gestion nuancée des news macro
- Événement HIGH impact dans les 60 min → NE PAS entrer, laisser passer
- Événement HIGH impact dans les 60-180 min → entrer uniquement si setup exceptionnel
  ET clôturer AVANT l'annonce (ou minimum SL très serré)
- Événement MEDIUM/LOW impact → analyser normalement mais tenir compte
- Position ouverte + macro HIGH imminente → sortir préventivement

## Confluence recommandée pour ENTER
Chercher au minimum 3-4 éléments alignés:
1. Biais directionnel D1/H4 (structure + EMA)
2. Zone d'intérêt touchée (support/résistance/order block/FVG)
3. Signal d'action de prix M15 (engulfing/pin bar/BOS/CHoCH)
4. Volume ou activité cohérents
5. DXY dans la direction inverse (haussière XAUUSD = DXY baissier ou neutre)
6. VWAP session comme support/résistance dynamique
7. Retracement Fibonacci pertinent (61.8% ou 78.6% souvent respectés)

Plus tu as de confluences, plus la confiance monte.

## Gestion du R:R
- R:R minimum recommandé: 2.0 (mais tu peux descendre à 1.5 si setup exceptionnel)
- SL structurel (au-delà de la zone qui invalide le setup + buffer ATR)
- TP au prochain niveau structurel logique (résistance/support suivante)

## Gestion des positions ouvertes
Tu recevras dans le dossier les positions actuellement ouvertes. Selon le contexte:
- **HOLD**: la position évolue normalement, rien à faire
- **REDUCE**: le prix a parcouru 1R (= la distance du risque en profit) → propose
  de fermer 50% et remonter le SL au breakeven. Utilise sl_propose pour le
  nouveau SL au point d'entrée.
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
  sur un setup incertain.
- **Priorité #2**: qualité de décision > fréquence. Si le contexte n'est pas clair,
  HOLD est TOUJOURS une réponse valide et souvent la meilleure.

## Cohérence avec tes décisions précédentes
Le dossier inclut tes 10 dernières décisions. Utilise-les pour:
- Éviter les contradictions (ex: passer de BUY à SELL en 15 min sans justification)
- Détecter l'over-trading (si tu vois 5 HOLD récents avec raisonnements similaires,
  reste cohérent tant que le contexte n'a pas vraiment changé)
- Reconnaître les setups déjà validés (si tu attendais une cassure et qu'elle
  vient d'avoir lieu, entre; ne "oublie" pas ce que tu attendais)

## Format de réponse OBLIGATOIRE

Tu dois retourner UNIQUEMENT un objet JSON valide (aucun texte autour, aucun
bloc de code markdown, juste le JSON brut) au format:

{
  "decision": "HOLD" | "ENTER" | "EXIT" | "REDUCE",
  "direction": "BUY" | "SELL" | null,
  "sl_propose": <float ou null>,
  "tp_propose": <float ou null>,
  "raisonnement": "<3-8 phrases: analyse du contexte, confluences identifiées, justification de la décision>",
  "confiance": "haute" | "moyenne" | "basse",
  "risques_identifies": ["<risque 1>", "<risque 2>", ...],
  "confluences_utilisees": ["<confluence 1>", "<confluence 2>", ...]
}

Règles de format strictes:
- HOLD: direction=null, sl_propose=null, tp_propose=null
- ENTER: direction (BUY/SELL), sl_propose, tp_propose OBLIGATOIRES
- EXIT: direction=null (ferme la position existante), sl/tp=null
- REDUCE: direction=null, sl_propose=nouveau SL au breakeven, tp_propose=null

## Rappel critique
- Si tu proposes ENTER mais que le risk manager voit une violation (plafond
  journalier atteint, position corrélée, R:R trop bas, etc.), ta décision sera
  automatiquement forcée en HOLD. C'est un filet, pas une opposition à toi.
- Reconnais les règles et respecte-les — ne tente PAS de les contourner.
- En cas de doute: HOLD.
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
