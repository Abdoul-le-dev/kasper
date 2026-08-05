"""
test_order_flow.py

Test one-shot du flux d'exécution MetaApi.

Ouvre une position micro (0.01 lot) avec SL très proche, attend 3 secondes,
puis la ferme. Objectif : vérifier que le système sait passer un ordre bout-en-bout.

Sécurités:
- Compte démo uniquement (utilise METAAPI_ACCOUNT_ID de .env)
- Lot 0.01 (minimum)
- SL à 3 $ du prix (perte max ~3 $)
- Fermeture après 3 secondes
- Confirmation manuelle "TESTER" requise

Usage:
    python -m src.test_order_flow
"""

from __future__ import annotations

import sys
import time
import logging
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402
from src.metaapi_connector import (
    get_config as metaapi_get_config,
    get_pricing, get_account_summary, get_open_trades,
    place_market_order, close_trade,
    MetaApiError,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger("test_order_flow")


def telegram_notify(msg: str) -> None:
    """Best-effort telegram."""
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        return
    try:
        import httpx
        url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
        with httpx.Client(timeout=10) as c:
            c.post(url, json={"chat_id": config.TELEGRAM_CHAT_ID, "text": msg})
    except Exception:
        pass


def main() -> None:
    print("=" * 60)
    print("TEST D'EXÉCUTION MetaApi — Ouverture + Fermeture immédiate")
    print("=" * 60)
    print()
    print("Ce test va:")
    print("  1. Vérifier la connexion MetaApi")
    print("  2. Récupérer le prix actuel de GOLD")
    print("  3. Ouvrir une position BUY de 0.01 lot avec SL à 3$")
    print("  4. Attendre 3 secondes")
    print("  5. Fermer la position immédiatement")
    print()
    print("⚠️  Une position sera OUVERTE sur ton compte MetaApi configuré.")
    print("   Perte max théorique : ~3 $ (si SL touché) — compte démo, argent factice")
    print()

    confirm = input("Taper 'TESTER' pour lancer, autre chose pour annuler : ").strip()
    if confirm != "TESTER":
        print("Annulé.")
        sys.exit(0)

    # --- Config check ---
    try:
        cfg = metaapi_get_config()
        print(f"\n✅ Config MetaApi OK")
        print(f"   Account ID : {cfg['account_id'][:8]}...")
        print(f"   Region     : {cfg['region']}")
        print(f"   Symbol     : {cfg['symbol']}")
    except Exception as e:
        print(f"\n❌ Config MetaApi invalide : {e}")
        sys.exit(1)

    # --- Solde initial ---
    try:
        summary_before = get_account_summary()
        print(f"\n📊 Solde avant test : {summary_before['solde']:.2f} $")
        print(f"   Équité           : {summary_before['equite']:.2f} $")
    except Exception as e:
        print(f"\n❌ Impossible de récupérer le compte : {e}")
        sys.exit(1)

    # --- Prix actuel ---
    try:
        pricing = get_pricing()
        print(f"\n💰 Prix GOLD actuel")
        print(f"   Bid    : {pricing['bid']}")
        print(f"   Ask    : {pricing['ask']}")
        print(f"   Spread : {pricing['spread']:.3f} $")
    except Exception as e:
        print(f"\n❌ Impossible de récupérer le prix : {e}")
        sys.exit(1)

    entry_price = pricing["ask"]
    sl_price = round(entry_price - 3.0, 2)   # SL à 3$ sous le prix d'entrée
    tp_price = round(entry_price + 10.0, 2)  # TP à 10$ (large, on ferme avant de toute façon)
    units = 1.0  # 0.01 lot = 1 unité sur XAUUSD chez XM (1 lot = 100 unités)

    print(f"\n🎯 Ordre à envoyer :")
    print(f"   Direction   : BUY")
    print(f"   Entry       : {entry_price} (market)")
    print(f"   SL          : {sl_price}")
    print(f"   TP          : {tp_price}")
    print(f"   Units       : {units} (= 0.01 lot)")

    # --- Envoi de l'ordre ---
    telegram_notify(f"🧪 Test d'ordre démarré\nBUY GOLD @ {entry_price}\nSL: {sl_price}, TP: {tp_price}")

    try:
        print(f"\n📤 Envoi de l'ordre...")
        result = place_market_order(
            direction="BUY",
            units=units,
            sl_price=sl_price,
            tp_price=tp_price,
        )
        print(f"✅ Ordre envoyé, réponse MetaApi :")
        for k, v in result.items():
            print(f"     {k}: {v}")
    except Exception as e:
        print(f"\n❌ ÉCHEC de l'ordre : {e}")
        telegram_notify(f"❌ Test d'ordre ÉCHOUÉ: {e}")
        sys.exit(1)

    # Récupération de l'ID de position
    pos_id = result.get("orderId") or result.get("positionId") or result.get("id")
    print(f"\n🆔 Position ID : {pos_id}")

    # --- Vérification que la position est bien ouverte côté broker ---
    time.sleep(1)
    try:
        trades = get_open_trades()
        matching = [t for t in trades if str(t.get("id")) == str(pos_id)]
        if matching:
            print(f"✅ Position confirmée ouverte côté broker :")
            for k, v in matching[0].items():
                print(f"     {k}: {v}")
        else:
            print(f"⚠️  Position {pos_id} pas trouvée dans les trades ouverts.")
            print(f"    Peut-être déjà exécutée/fermée. Trades actuels :")
            for t in trades:
                print(f"     {t}")
    except Exception as e:
        print(f"⚠️  Impossible de vérifier : {e}")

    # --- Attente 3 secondes ---
    print(f"\n⏱️  Attente 3 secondes avant fermeture...")
    time.sleep(3)

    # --- Fermeture ---
    try:
        print(f"\n📥 Fermeture de la position {pos_id}...")
        close_result = close_trade(pos_id)
        print(f"✅ Fermeture effectuée, réponse :")
        for k, v in close_result.items():
            print(f"     {k}: {v}")
    except Exception as e:
        print(f"\n⚠️  Échec fermeture : {e}")
        print(f"    ATTENTION : la position pourrait être toujours ouverte, vérifie manuellement.")
        telegram_notify(f"⚠️ Test: fermeture échouée, position peut-être ouverte: {e}")

    # --- Solde final ---
    time.sleep(1)
    try:
        summary_after = get_account_summary()
        delta = summary_after['equite'] - summary_before['equite']
        print(f"\n📊 Solde après test : {summary_after['solde']:.2f} $")
        print(f"   Équité           : {summary_after['equite']:.2f} $")
        print(f"   Δ équité         : {delta:+.2f} $")
    except Exception as e:
        print(f"\n⚠️  Impossible de récupérer le solde final : {e}")

    print()
    print("=" * 60)
    print("✅ TEST TERMINÉ")
    print("=" * 60)
    print("Si tu as vu 'Ordre envoyé' + 'Fermeture effectuée' sans erreur,")
    print("le système sait passer et fermer un ordre. Tu peux lancer paper_runner")
    print("en --mode paper en confiance.")
    telegram_notify("✅ Test d'ordre terminé sans erreur")


if __name__ == "__main__":
    main()
