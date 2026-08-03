"""
claude_advisor.py

Module qui envoie le dossier d'analyse à Claude Opus 4.8 via l'API Anthropic
et récupère sa décision de trading.

Architecture B pure : Claude est le décideur à chaque cycle. Le risk manager
(risk_manager.py) reste au-dessus et peut refuser une décision de Claude si
elle viole les règles non négociables.

Configuration (variables d'environnement) :
    ANTHROPIC_API_KEY      : clé API Anthropic
    CLAUDE_MODEL           : "claude-opus-4-8" (défaut) ou surcharge
    CLAUDE_TIMEOUT_SECONDS : timeout d'un appel (défaut 30)

Ce module est testable de façon isolée grâce à l'injection de `client`
(httpx.Client) — aucun appel réseau réel dans les tests.
"""

import os
import json
import logging
from typing import Dict, Any, Optional

import httpx

logger = logging.getLogger("trading_engine.claude_advisor")

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
DEFAULT_MODEL = "claude-opus-4-8"
DEFAULT_TIMEOUT_SECONDS = 30
DEFAULT_MAX_TOKENS = 1024
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


# --- Prompt système : les instructions permanentes envoyées à chaque appel ---

SYSTEM_PROMPT = """Tu es le décideur d'un système de trading autonome sur XAUUSD (or).
Capital initial: 100$. Durée: 7 jours. Levier obligatoire.

Ta stratégie (figée, non négociable):
- Style: intraday/swing court, basé sur structure de marché + volatilité + action de prix
- Modèle hybride en 3 couches:
  1. Biais directionnel (D1/H4): tendance de fond, alignement EMA50/EMA200, structure
  2. Régime de volatilité (H1): IRV = ATR courant / moyenne ATR 20 périodes
  3. Déclencheur d'entrée (M15): action de prix + zone d'intérêt validée

Critères d'entrée (TOUS obligatoires):
- FQE (Filtre de Qualité d'Entrée) ≥ 4/5
- SCD (Score de Confluence Directionnelle) non nul et cohérent avec la direction
- IRV en régime "normal" (0.7-1.3) ou "expansion" (>1.3), JAMAIS en "compression" (<0.7)
- R:R (Risque:Rendement) calculé ≥ 2.0 avant l'entrée
- Zone d'intérêt touchée à moins de 0.3 × ATR

Gestion du risque (RÈGLES NON NÉGOCIABLES — le code refusera automatiquement toute décision qui les viole):
- Risque minimum par trade: 5$ (5% du capital)
- Perte maximale journalière: 25$ → arrêt total des entrées jusqu'au lendemain
- Maximum 2 positions simultanées, JAMAIS corrélées dans la même direction
- Maximum 5 trades perdants par jour
- R:R minimum: 2.0
- Le SL doit toujours être structurel (au-delà de la zone qui invalide le setup + buffer ATR)
- Le TP doit viser le prochain niveau structurel offrant R:R ≥ 2.0

Situations d'invalidation d'une position ouverte:
- Cassure NETTE (clôture, pas mèche) de la zone qui a justifié l'entrée → EXIT
- Publication macro majeure imminente (< 60 min) → EXIT
- IRV bascule > 2.0 sans structure claire → EXIT
- Divergence entre biais H4 et direction du trade → EXIT
- Sur atteinte de 1R (le prix a parcouru la distance du risque en profit): sortie partielle 50% + SL remonté au breakeven → REDUCE

Priorités de décision (dans l'ordre):
1. Survie du capital avant profit
2. Qualité de décision avant fréquence
3. Discipline avant intuition
4. Justification obligatoire (jamais d'action non justifiée)

TON RÔLE À CHAQUE APPEL:
Tu reçois un dossier d'analyse structuré (indicateurs calculés, zones détectées,
action de prix identifiée, état du compte, budget de risque restant).
Tu dois retourner UNIQUEMENT un objet JSON valide, sans texte autour, au format:

{
  "decision": "HOLD" | "ENTER" | "EXIT" | "REDUCE",
  "direction": "BUY" | "SELL" | null,
  "sl_propose": <float ou null>,
  "tp_propose": <float ou null>,
  "raisonnement": "<3-5 phrases expliquant l'analyse et la décision>",
  "confiance": "haute" | "moyenne" | "basse",
  "risques_identifies": ["<risque 1>", "<risque 2>"]
}

Règles de format strictes:
- Pour HOLD: direction=null, sl_propose=null, tp_propose=null
- Pour ENTER: direction, sl_propose, tp_propose obligatoires
- Pour EXIT: direction=null (on ferme la position existante), sl/tp=null
- Pour REDUCE: direction=null, sl_propose=nouveau SL au breakeven, tp_propose=null

Rappel critique: le risk manager côté code va vérifier ta décision.
Si tu proposes ENTER mais qu'une règle est violée (R:R<2, perte du jour ≥25$,
position corrélée existante, etc.), la décision sera automatiquement forcée en HOLD.
Ne tente PAS de contourner ces règles — reconnais-les et respecte-les.

Si le contexte est ambigu ou insuffisant, la décision par défaut est HOLD.
Il vaut mieux rater une opportunité qu'entrer sur un setup incertain.
"""


def build_user_message(dossier: Dict[str, Any]) -> str:
    """
    Sérialise le dossier d'analyse en un message utilisateur lisible.
    Le dossier est passé sous forme JSON pour que Claude puisse le parser
    de façon fiable et raisonner sur chaque champ.
    """
    return (
        "Voici le dossier d'analyse pour le cycle actuel. Analyse-le "
        "rigoureusement et retourne UNIQUEMENT un objet JSON valide (aucun "
        "texte autour, aucun bloc de code markdown, juste le JSON brut).\n\n"
        f"```json\n{json.dumps(dossier, ensure_ascii=False, indent=2)}\n```"
    )


def _extract_json_from_response(text: str) -> Dict[str, Any]:
    """
    Parse la réponse de Claude et extrait l'objet JSON.
    Tolère les blocs markdown ```json ... ``` au cas où (même si le prompt
    demande du JSON brut, on est défensif).
    """
    text = text.strip()

    # Retirer les fences markdown si présentes
    if text.startswith("```"):
        # Retirer la première ligne (```json ou ```)
        lines = text.split("\n")
        lines = lines[1:]
        # Retirer la dernière ligne si c'est ```
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)

    # Trouver le premier { et le dernier } pour extraire le JSON même s'il y
    # a du texte parasite avant/après
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
    """
    Vérifie que la décision retournée par Claude a bien tous les champs
    requis et des valeurs cohérentes. Lève ClaudeAdvisorError sinon.
    """
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

    # Cohérence par type de décision
    if decision["decision"] == "ENTER":
        if decision["direction"] not in ("BUY", "SELL"):
            raise ClaudeAdvisorError("ENTER exige une direction BUY ou SELL")
        if decision["sl_propose"] is None or decision["tp_propose"] is None:
            raise ClaudeAdvisorError("ENTER exige sl_propose et tp_propose non nuls")


def default_hold_decision(reason: str) -> Dict[str, Any]:
    """
    Décision par défaut en cas d'échec technique (API down, timeout, JSON invalide).
    Sécurité maximale: HOLD.
    """
    return {
        "decision": "HOLD",
        "direction": None,
        "sl_propose": None,
        "tp_propose": None,
        "raisonnement": f"[Fallback automatique — décision par défaut car {reason}]",
        "confiance": "basse",
        "risques_identifies": [f"Fallback technique: {reason}"],
    }


def ask_claude(
    dossier: Dict[str, Any],
    config: Optional[Dict[str, Any]] = None,
    client: Optional[httpx.Client] = None,
) -> Dict[str, Any]:
    """
    Envoie le dossier d'analyse à Claude Opus et retourne sa décision.

    En cas d'erreur (config manquante, API down, timeout, JSON invalide,
    validation échouée), retourne une décision HOLD par défaut plutôt que
    de lever une exception — la sécurité prime, le trading continue.
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
