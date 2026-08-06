

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402
from src.metaapi_connector import (
    get_pricing, get_open_trades, place_market_order,
    get_config as metaapi_get_config,
)
from src.shared import (
    telegram_send, journal_append, is_kill_switch_active, kill_switch_message,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger("trade")

PIP_SIZE = 0.1  # 1 pip XAUUSD = 0.10 $
PIP_VALUE_PER_LOT = 10.0


def in_trading_session() -> bool:
    """True si l'heure UTC actuelle est dans Londres OU NY."""
    h = datetime.now(timezone.utc).hour
    return config.LONDON_START_HOUR <= h < config.NY_END_HOUR  # 07-22 UTC


def format_pips(distance: float) -> str:
    return f"{distance / PIP_SIZE:.0f} pips"


def compose_open_message(direction: str, entry: float, sl: float, tp: float,
                        lot: float, rr: float, why: str) -> str:
    sl_dist = abs(entry - sl)
    tp_dist = abs(tp - entry)
    arrow = "📈" if direction == "BUY" else "📉"
    return (
        f"{arrow} *SIGNAL {direction} — GOLD*\n\n"
        f"Entry : `{entry:.2f}`\n"
        f"SL    : `{sl:.2f}`  (-{format_pips(sl_dist)})\n"
        f"TP    : `{tp:.2f}`  (+{format_pips(tp_dist)})\n"
        f"Lot   : `{lot}`\n"
        f"R:R   : `{rr:.2f}`\n\n"
        f"_Contexte_ : {why}"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("direction", choices=["buy", "sell", "BUY", "SELL"])
    parser.add_argument("--price", type=float, required=True, help="Prix cible (protection ±5$)")
    parser.add_argument("--lot", type=float, required=True, help="Taille en lots (ex: 0.05)")
    parser.add_argument("--sl", type=float, required=True)
    parser.add_argument("--tp", type=float, required=True)
    parser.add_argument("--why", type=str, required=True, help="Contexte (publié tel quel)")
    parser.add_argument("--force", action="store_true",
                        help="Ignore le check session (à éviter)")
    args = parser.parse_args()

    direction = args.direction.upper()

    # --- Session check ---
    if not args.force and not in_trading_session():
        print("❌ Hors session Londres/NY (07-22 UTC). Utilise --force pour outrepasser.")
        sys.exit(1)

    # --- Config MetaApi ---
    try:
        metaapi_get_config()
    except Exception as e:
        print(f"❌ Config MetaApi invalide: {e}")
        sys.exit(1)

    # --- Kill switch ---
    if is_kill_switch_active():
        print("🛑 Kill switch actif — budget de risque quotidien atteint.")
        telegram_send(kill_switch_message())
        journal_append({
            "kind": "signal_rejected_kill_switch",
            "source": "direct",
            "direction": direction,
            "price_requested": args.price,
            "why": args.why,
        })
        sys.exit(1)

    # --- Prix marché ---
    try:
        pricing = get_pricing()
    except Exception as e:
        print(f"❌ Impossible de récupérer le prix: {e}")
        sys.exit(1)

    market_price = pricing["ask"] if direction == "BUY" else pricing["bid"]
    price_delta = abs(args.price - market_price)

    if price_delta > config.MANUAL_PRICE_TOLERANCE_USD:
        print(f"❌ Écart trop grand : prix demandé {args.price}, marché {market_price:.2f} (Δ {price_delta:.2f} $)")
        print(f"   Tolérance : ±{config.MANUAL_PRICE_TOLERANCE_USD} $. Ordre annulé.")
        sys.exit(1)

    entry = market_price  # exécution au marché

    # --- Validation SL/TP ---
    if direction == "BUY":
        sl_dist = entry - args.sl
        tp_dist = args.tp - entry
    else:
        sl_dist = args.sl - entry
        tp_dist = entry - args.tp

    if sl_dist <= 0:
        print(f"❌ SL du mauvais côté (direction {direction}, entry {entry}, sl {args.sl})")
        sys.exit(1)
    if tp_dist <= 0:
        print(f"❌ TP du mauvais côté (direction {direction}, entry {entry}, tp {args.tp})")
        sys.exit(1)

    rr = tp_dist / sl_dist
    if rr < config.MIN_RISK_REWARD:
        print(f"❌ R:R {rr:.2f} < minimum {config.MIN_RISK_REWARD}")
        sys.exit(1)

    # --- Vérif une position direct à la fois ---
    from src.shared import journal_read
    from datetime import timedelta
    today_utc_midnight = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    recent = journal_read(after=today_utc_midnight)
    open_direct = [e for e in recent if e.get("kind") == "position_opened" and e.get("source") == "direct"]
    closed_direct = [e for e in recent if e.get("kind", "").startswith("position_closed") and e.get("source") == "direct"]
    if len(open_direct) > len(closed_direct):
        print("⚠️  Une position 'direct' est déjà ouverte aujourd'hui. Attends sa fermeture.")
        sys.exit(1)

    # --- Envoi de l'ordre ---
    print(f"\n📤 Envoi ordre {direction} @ {entry:.2f}  SL {args.sl:.2f}  TP {args.tp:.2f}  Lot {args.lot}")
    print(f"   R:R {rr:.2f}  |  Contexte : {args.why}")
    print()

    try:
        result = place_market_order(
            direction=direction,
            units=args.lot,
            sl=args.sl,
            tp=args.tp,
        )
        pos_id = result.get("orderId") or result.get("positionId") or result.get("id")
    except Exception as e:
        print(f"❌ Ordre refusé par le broker : {e}")
        journal_append({
            "kind": "order_failed",
            "source": "direct",
            "direction": direction,
            "error": str(e),
            "why": args.why,
        })
        telegram_send(f"⚠️ Ordre {direction} rejeté par le broker : `{e}`")
        sys.exit(1)

    print(f"✅ Position ouverte, id: {pos_id}")

    # --- Journal ---
    journal_append({
        "kind": "position_opened",
        "source": "direct",
        "position_id": str(pos_id),
        "direction": direction,
        "entry": round(entry, 2),
        "sl": args.sl,
        "tp": args.tp,
        "lot": args.lot,
        "rr": round(rr, 2),
        "why": args.why,
    })

    # --- Telegram (ton "algo") ---
    msg = compose_open_message(direction, entry, args.sl, args.tp, args.lot, rr, args.why)
    telegram_send(msg)
    print("✅ Signal publié sur Telegram.")


if __name__ == "__main__":
    main()
