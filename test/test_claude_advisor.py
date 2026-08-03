"""
Tests isolés de claude_advisor.py.
Aucun appel réel à l'API Anthropic — tous les échanges HTTP sont simulés via
httpx.MockTransport.
"""

import sys
import os
import json
import pytest
import httpx

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from trading_engine import claude_advisor as ca


CONFIG = {"api_key": "fake-key", "model": "claude-opus-4-8", "timeout": 30.0}


def mock_client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def anthropic_response(text_content):
    """Construit une réponse Anthropic factice au format attendu."""
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": text_content}],
        "model": "claude-opus-4-8",
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 500, "output_tokens": 100},
    }


# --- Configuration ---

def test_get_config_raises_when_missing(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(ca.ClaudeAdvisorConfigError):
        ca.get_config()


def test_get_config_reads_env(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("CLAUDE_MODEL", "claude-sonnet-4-6")
    monkeypatch.setenv("CLAUDE_TIMEOUT_SECONDS", "60")
    cfg = ca.get_config()
    assert cfg["api_key"] == "test-key"
    assert cfg["model"] == "claude-sonnet-4-6"
    assert cfg["timeout"] == 60.0


def test_get_config_uses_defaults(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.delenv("CLAUDE_MODEL", raising=False)
    monkeypatch.delenv("CLAUDE_TIMEOUT_SECONDS", raising=False)
    cfg = ca.get_config()
    assert cfg["model"] == "claude-opus-4-8"
    assert cfg["timeout"] == 30.0


# --- Extraction JSON ---

def test_extract_json_from_pure_json():
    text = '{"decision": "HOLD", "direction": null}'
    result = ca._extract_json_from_response(text)
    assert result["decision"] == "HOLD"


def test_extract_json_from_markdown_fence():
    text = '```json\n{"decision": "ENTER", "direction": "BUY"}\n```'
    result = ca._extract_json_from_response(text)
    assert result["decision"] == "ENTER"


def test_extract_json_with_surrounding_text():
    text = 'Voici la décision:\n{"decision": "HOLD"}\nFin.'
    result = ca._extract_json_from_response(text)
    assert result["decision"] == "HOLD"


def test_extract_json_no_json_raises():
    with pytest.raises(ca.ClaudeAdvisorError):
        ca._extract_json_from_response("Aucun JSON ici")


def test_extract_json_invalid_json_raises():
    with pytest.raises(ca.ClaudeAdvisorError):
        ca._extract_json_from_response('{"decision": "HOLD"')  # accolade manquante


# --- Validation ---

def valid_hold():
    return {
        "decision": "HOLD", "direction": None,
        "sl_propose": None, "tp_propose": None,
        "raisonnement": "test", "confiance": "moyenne",
        "risques_identifies": [],
    }


def valid_enter():
    return {
        "decision": "ENTER", "direction": "BUY",
        "sl_propose": 1995.0, "tp_propose": 2010.0,
        "raisonnement": "confluence forte", "confiance": "haute",
        "risques_identifies": ["macro news dans 2h"],
    }


def test_validate_valid_hold_passes():
    ca.validate_decision(valid_hold())


def test_validate_valid_enter_passes():
    ca.validate_decision(valid_enter())


def test_validate_missing_field_raises():
    d = valid_hold()
    del d["decision"]
    with pytest.raises(ca.ClaudeAdvisorError):
        ca.validate_decision(d)


def test_validate_invalid_decision_raises():
    d = valid_hold()
    d["decision"] = "BUY_NOW"
    with pytest.raises(ca.ClaudeAdvisorError):
        ca.validate_decision(d)


def test_validate_invalid_direction_raises():
    d = valid_enter()
    d["direction"] = "LONG"
    with pytest.raises(ca.ClaudeAdvisorError):
        ca.validate_decision(d)


def test_validate_enter_without_direction_raises():
    d = valid_enter()
    d["direction"] = None
    with pytest.raises(ca.ClaudeAdvisorError):
        ca.validate_decision(d)


def test_validate_enter_without_sl_raises():
    d = valid_enter()
    d["sl_propose"] = None
    with pytest.raises(ca.ClaudeAdvisorError):
        ca.validate_decision(d)


def test_validate_invalid_confidence_raises():
    d = valid_hold()
    d["confiance"] = "extrême"
    with pytest.raises(ca.ClaudeAdvisorError):
        ca.validate_decision(d)


def test_validate_risques_not_list_raises():
    d = valid_hold()
    d["risques_identifies"] = "un risque"  # doit être une liste
    with pytest.raises(ca.ClaudeAdvisorError):
        ca.validate_decision(d)


# --- ask_claude : cas nominal ---

def test_ask_claude_returns_valid_decision():
    def handler(request):
        # Vérifier que les headers sont corrects
        assert request.headers["x-api-key"] == "fake-key"
        assert request.headers["anthropic-version"] == "2023-06-01"
        body = json.loads(request.content)
        assert body["model"] == "claude-opus-4-8"
        assert "system" in body
        assert "Tu es le décideur" in body["system"]
        return httpx.Response(200, json=anthropic_response(json.dumps(valid_hold())))

    client = mock_client(handler)
    dossier = {"prix": {"actuel": 2000.0}, "scd": 0}
    result = ca.ask_claude(dossier, config=CONFIG, client=client)
    assert result["decision"] == "HOLD"


def test_ask_claude_handles_enter_decision():
    def handler(request):
        return httpx.Response(200, json=anthropic_response(json.dumps(valid_enter())))

    client = mock_client(handler)
    result = ca.ask_claude({}, config=CONFIG, client=client)
    assert result["decision"] == "ENTER"
    assert result["direction"] == "BUY"
    assert result["sl_propose"] == 1995.0


# --- ask_claude : fallback sur HOLD ---

def test_ask_claude_falls_back_to_hold_on_api_error():
    def handler(request):
        return httpx.Response(500, text='{"error": "internal"}')

    client = mock_client(handler)
    result = ca.ask_claude({}, config=CONFIG, client=client)
    assert result["decision"] == "HOLD"
    assert "500" in result["raisonnement"]


def test_ask_claude_falls_back_to_hold_on_401():
    def handler(request):
        return httpx.Response(401, text='{"error": "unauthorized"}')

    client = mock_client(handler)
    result = ca.ask_claude({}, config=CONFIG, client=client)
    assert result["decision"] == "HOLD"


def test_ask_claude_falls_back_to_hold_on_timeout():
    def handler(request):
        raise httpx.TimeoutException("timeout simulated")

    client = mock_client(handler)
    result = ca.ask_claude({}, config=CONFIG, client=client)
    assert result["decision"] == "HOLD"
    assert "timeout" in result["raisonnement"].lower()


def test_ask_claude_falls_back_to_hold_on_network_error():
    def handler(request):
        raise httpx.ConnectError("connexion refusée")

    client = mock_client(handler)
    result = ca.ask_claude({}, config=CONFIG, client=client)
    assert result["decision"] == "HOLD"


def test_ask_claude_falls_back_to_hold_on_invalid_json():
    def handler(request):
        return httpx.Response(200, json=anthropic_response("Ceci n'est pas du JSON"))

    client = mock_client(handler)
    result = ca.ask_claude({}, config=CONFIG, client=client)
    assert result["decision"] == "HOLD"
    assert "invalide" in result["raisonnement"].lower()


def test_ask_claude_falls_back_to_hold_on_invalid_decision_structure():
    """Claude retourne un JSON valide mais qui ne respecte pas notre schéma."""
    bad_decision = {"decision": "BUY_STRONG", "direction": "LONG"}  # décision invalide

    def handler(request):
        return httpx.Response(200, json=anthropic_response(json.dumps(bad_decision)))

    client = mock_client(handler)
    result = ca.ask_claude({}, config=CONFIG, client=client)
    assert result["decision"] == "HOLD"


def test_ask_claude_falls_back_to_hold_when_config_missing(monkeypatch):
    """Sans clé API, on doit avoir un HOLD par défaut sans planter."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    result = ca.ask_claude({})
    assert result["decision"] == "HOLD"
    assert "config" in result["raisonnement"].lower()


def test_ask_claude_response_without_text_content():
    """Anthropic peut retourner une réponse sans bloc texte (rare mais possible)."""
    def handler(request):
        return httpx.Response(200, json={"content": [], "model": "claude-opus-4-8"})

    client = mock_client(handler)
    result = ca.ask_claude({}, config=CONFIG, client=client)
    assert result["decision"] == "HOLD"


def test_default_hold_decision_structure():
    d = ca.default_hold_decision("test raison")
    assert d["decision"] == "HOLD"
    assert d["direction"] is None
    assert d["sl_propose"] is None
    assert d["confiance"] == "basse"
    assert "test raison" in d["raisonnement"]
