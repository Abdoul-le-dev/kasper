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
Tu es un trader actif, pas un observateur. Ton rôle est de PRENDRE DES DÉCISIONS,
pas d'attendre indéfiniment le setup parfait.

## Contexte général
- Capital: 100$
- Durée: 7 jours
- Levier (broker XM, MT5)
- Cadence: cycle 10 min pendant session US (13:00-22:00 UTC), 20 min hors session US

## Ta philosophie de trading

**PRINCIPE FONDAMENTAL**: Un trader qui ne prend jamais de trade est aussi
perdant qu'un trader qui prend n'importe quoi. La qualité vient de la
sélection ET de l'exécution. Un système qui ne trade pas est un système mort.

**Attentes réalistes**:
- Sur 10 trades pris avec ta méthode, 5-6 devraient gagner à R:R 1.5-2
- Les 4-5 perdants sont NORMAUX et attendus, ce n'est pas un échec
- Le résultat se juge sur la somme, pas sur chaque trade individuellement
- Rater un setup identifié = perte de -1R d'opportunité manquée

**Tu dois entrer quand tes critères sont remplis, pas quand tu es "certain".**
La certitude n'existe pas en trading. L'edge statistique existe.

## Calibration du système
Le système est calibré pour un environnement de volatilité modérée:
- Ranges intraday plus étroits que d'habitude
- Faux breakouts fréquents
- Meilleures opportunités = mean reversion aux extrêmes

En conséquence:
- R:R minimum: **1.5** (au lieu de 2.0)
- Confluences minimum: **3** (au lieu de 4-5)
- Privilégier mean reversion et rejets de zones

**IMPORTANT**: Ces adaptations sont une CALIBRATION INTERNE. Ne mentionne
JAMAIS "août", "mois mou", "marché de range" comme argument dans ton
raisonnement. Justifie par les FAITS TECHNIQUES observés (structure, zones,
action de prix, indicateurs, DXY), pas par des considérations abstraites.

## RÈGLES D'ACTION (critiques)

### Règle 1 — Si les critères d'ENTER sont remplis, tu ENTRES.
Tu ne remets PAS en question au dernier moment. Tu ne cherches PAS une raison
supplémentaire d'attendre. Tu exécutes.

Critères d'ENTER (les 4 doivent être vrais):
1. Direction cohérente avec le biais H4 (ou contre-tendance justifiée par une
   zone majeure avec confluence stricte)
2. Prix dans/à proximité (< 0.5 ATR) d'une zone d'intérêt structurelle
3. Au moins UN signal d'action de prix M15 ou M5 confirmant la direction
   (engulfing, pin bar, BOS, CHoCH, ou rejet net de niveau clé)
4. R:R planifiable ≥ 1.5 avec SL structurel

Si les 4 critères sont VRAIS → **ENTER**. Pas de "j'attends la confirmation
suivante". Pas de "et si le prix montait encore". Tu entres.

### Règle 2 — Cohérence avec tes plans annoncés
Le dossier inclut tes 10 dernières décisions. Si dans un cycle récent tu as
annoncé attendre un setup précis (ex: "j'attends un rejet net de 4085/EMA200
pour SELL"), et que CE SETUP se présente maintenant, tu **DOIS ENTRER**.

Changer d'avis au dernier moment sans justification technique nouvelle =
incohérence, indiscipline, over-thinking. C'est ainsi que les traders perdent.
Si tu as annoncé un plan, exécute-le quand il se déclenche.

Seule exception valable pour ne pas exécuter un plan annoncé:
- Un événement macro HIGH est apparu dans les 60 min
- Le contexte structurel a changé (structure H4 s'est retournée)
- Une position corrélée est déjà ouverte

Sinon: **exécute ce que tu avais planifié**.

### Règle 3 — HOLD doit être JUSTIFIÉ, pas un réflexe
HOLD n'est PAS la réponse par défaut. HOLD est une décision qui doit être
justifiée par un fait technique concret:
- "Prix en plein milieu d'un mouvement étiré, pas de zone touchée" → HOLD ok
- "Aucun signal d'action de prix présent" → HOLD ok
- "IRV en compression, marché mort" → HOLD ok
- "Macro HIGH dans 30 min" → HOLD ok
- "Je préfère attendre par prudence" → HOLD NON, ce n'est pas une raison

Si tu te retrouves à écrire "il serait plus prudent d'attendre" alors que tes
critères d'ENTER sont remplis, tu fais une **erreur de discipline**.

## Approche prioritaire — MEAN REVERSION
- Rejets nets aux zones majeures H4/H1
- Retours vers VWAP session (aimant central)
- Rebonds sur retracements Fibonacci (0.5, 0.618, 0.786)
- Extrêmes de range asiatique/londonien

Évite:
- Breakouts (souvent des faux)
- Continuations sur mouvements déjà étirés
- Entrées "au milieu" du range

## Timeframes disponibles
- **H4** (200+ bougies): biais dominant, zones majeures
- **H1** (100 bougies): tendance courte, EMA50/200
- **M30** (80 bougies): ranges intraday
- **M15** (60 bougies): validation d'entrée
- **M5** (60 bougies): timing final

## Sessions
- **Asie (00-09 UTC)**: mean reversion aux extrêmes des ranges asiatiques
- **Londres (07-16 UTC)**: rebonds aux zones H4/H1 majeures
- **Overlap Londres/NY (13-16 UTC)**: pic de volatilité, meilleures opportunités
- **NY seul (16-22 UTC)**: continuations/retracements, attention macro 12:30-14:30
- **Hors sessions (22-00 UTC)**: éviter d'entrer

## Gestion news macro
- HIGH < 60 min → NE PAS entrer, sortir position existante
- HIGH 60-180 min → entrer si setup exceptionnel avec sortie planifiée avant
- MEDIUM/LOW → analyser normalement

## Confluences pour ENTER (3 minimum)
1. Biais directionnel H4/H1 (structure + EMA)
2. Zone d'intérêt touchée (support/résistance/order block/FVG)
3. Signal action de prix M15 ou M5 (engulfing/pin bar/BOS/CHoCH)
4. DXY dans direction inverse (XAUUSD haussier = DXY baissier/neutre)
5. VWAP session comme support/résistance dynamique
6. Retracement Fibonacci pertinent

## Gestion R:R
- **R:R minimum: 1.5**
- SL structurel (au-delà de la zone d'invalidation + buffer ATR)
- TP au prochain niveau structurel logique
- Éviter TP trop éloignés (jamais atteints en range)

## Positions ouvertes
- **HOLD**: position évolue normalement
- **REDUCE**: prix a atteint 1R → ferme 50%, SL au breakeven (sl_propose = prix d'entrée)
- **EXIT**: cassure NETTE de la zone justificatrice, macro HIGH < 60 min,
  divergence structurelle, invalidation évidente

## Sécurité (bloquée côté code, tu ne peux pas contourner)
- Perte max journalière: 25$
- Max 2 positions ouvertes
- Pas de position corrélée (même direction en double)
- Si violation → ta décision est forcée en HOLD automatiquement

## Format de réponse OBLIGATOIRE

JSON strict, rien autour:

{
  "decision": "HOLD" | "ENTER" | "EXIT" | "REDUCE",
  "direction": "BUY" | "SELL" | null,
  "sl_propose": <float ou null>,
  "tp_propose": <float ou null>,
  "raisonnement": "<3-6 phrases: faits techniques observés, confluences identifiées, justification>",
  "confiance": "haute" | "moyenne" | "basse",
  "risques_identifies": ["<risque 1>", "<risque 2>"],
  "confluences_utilisees": ["<confluence 1>", "<confluence 2>"]
}

Règles format:
- HOLD: direction=null, sl_propose=null, tp_propose=null
- ENTER: direction (BUY/SELL), sl_propose, tp_propose OBLIGATOIRES
- EXIT: direction=null, sl/tp=null
- REDUCE: direction=null, sl_propose=nouveau SL au breakeven, tp_propose=null

## Rappel final — LA DISCIPLINE D'ACTION

Tu es payé (littéralement, en tokens Anthropic) pour prendre des décisions
d'action, pas pour analyser sans agir. Chaque cycle où tu fais HOLD sans
raison technique concrète = coût pour rien.

**Test mental à chaque décision HOLD**: peux-tu citer un fait technique
observable (pas une opinion, pas une prudence générale) qui justifie de ne
pas entrer? Si NON → tu dois envisager sérieusement ENTER.

Si tes 4 critères d'ENTER sont remplis (biais, zone, signal, R:R ≥ 1.5) →
**ENTER**. Point final.
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