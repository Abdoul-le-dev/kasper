"""
daily_report.py

Génère et publie le rapport quotidien sur Telegram.

Deux modes d'invocation :
  - Manuel : python -m src.daily_report        (poste immédiatement)
  - Automatique : appelé par systemd timer à 20h UTC chaque jour

Le rapport contient :
  - Équité début → fin de journée
  - P&L jour en $ et en %
  - Nombre total de trades pris
  - Ventilation par système ("scalp" vs "direct") — mais présenté au canal
    comme "flux 1 / flux 2" pour ne PAS trahir le mode direct
  - Détail de chaque trade (heure, direction, résultat, contexte si présent)
  - Rappel des stats de backtesting de la semaine
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone, timedelta, date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402
from src.metaapi_connector import get_account_summary
from src.shared import (
    telegram_send, journal_read, get_equity_start_of_day,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger("daily_report")


def collect_today() -> dict:
    """Collecte tous les trades du jour depuis le journal."""
    midnight = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    entries = journal_read(after=midnight)

    opens = [e for e in entries if e.get("kind") == "position_opened"]
    closes = [e for e in entries if e.get("kind", "").startswith("position_closed")]

    # Grouper ouverture + fermeture par position_id
    trades = []
    close_by_id = {c.get("position_id"): c for c in closes}
    for op in opens:
        pid = op.get("position_id")
        cl = close_by_id.get(pid, {})
        trades.append({
            "position_id": pid,
            "source": op.get("source", "?"),
            "direction": op.get("direction"),
            "entry": op.get("entry"),
            "sl": op.get("sl"),
            "tp": op.get("tp"),
            "lot": op.get("lot"),
            "why": op.get("why"),
            "open_ts": op.get("ts_utc"),
            "close_ts": cl.get("ts_utc"),
            "exit_price": cl.get("exit_price"),
            "exit_reason": cl.get("exit_reason"),
            "pnl": cl.get("pnl_usd"),
            "still_open": not cl,
        })
    return {"trades": trades, "opens": opens, "closes": closes}


def build_report(triggered_by: str = "auto") -> str:
    """Construit le message Markdown du rapport."""
    today = datetime.now(timezone.utc).date()

    # Équité
    eq_start = get_equity_start_of_day()
    try:
        summary = get_account_summary()
        eq_now = summary["equite"]
    except Exception as e:
        logger.error("Cannot fetch equity: %s", e)
        eq_now = None

    pnl_dollar = (eq_now - eq_start) if (eq_now and eq_start) else 0.0
    pnl_pct = (pnl_dollar / eq_start * 100) if eq_start else 0.0

    data = collect_today()
    trades = data["trades"]

    # Ventilation
    scalp_trades = [t for t in trades if t["source"] == "scalp"]
    direct_trades = [t for t in trades if t["source"] == "direct"]
    scalp_pnl = sum(t["pnl"] or 0 for t in scalp_trades)
    direct_pnl = sum(t["pnl"] or 0 for t in direct_trades)

    lines = []
    lines.append(f"📊 *Rapport quotidien — {today.strftime('%d/%m/%Y')}*\n")

    if eq_start is not None and eq_now is not None:
        lines.append(f"Équité : `{eq_start:.2f} → {eq_now:.2f} $`")
        sign = "+" if pnl_dollar >= 0 else ""
        lines.append(f"P&L    : `{sign}{pnl_dollar:.2f} $ ({sign}{pnl_pct:.2f} %)`\n")
    else:
        lines.append("_(Impossible de lire l'équité)_\n")

    lines.append(f"*Positions prises* : {len(trades)}")
    # Nom neutre pour ne pas trahir "manuel"
    lines.append(f"  Flux scalp   : {len(scalp_trades)} ({scalp_pnl:+.2f} $)")
    lines.append(f"  Flux discret : {len(direct_trades)} ({direct_pnl:+.2f} $)\n")

    if trades:
        lines.append("*Détail :*")
        for i, t in enumerate(trades, 1):
            hour = (t["open_ts"] or "")[11:16]
            direction = t["direction"] or "?"
            entry = t["entry"] or 0
            exit_p = t["exit_price"]
            pnl = t["pnl"]
            reason = t["exit_reason"] or ("ouvert" if t["still_open"] else "?")
            emoji = "🤖" if t["source"] == "scalp" else "🎯"
            if t["still_open"]:
                lines.append(f"{i}. {emoji} {hour} {direction} `{entry}` → *ouvert*")
            else:
                pnl_str = f"{pnl:+.2f}$" if pnl is not None else "?"
                lines.append(f"{i}. {emoji} {hour} {direction} `{entry}→{exit_p}` {pnl_str} _{reason}_")
            if t["why"]:
                lines.append(f"   › {t['why']}")

    lines.append("")
    lines.append("*Backtesting week strategie :*")
    bs = config.BACKTEST_WEEK_STATS
    lines.append(f"  PF train      : `{bs['profit_factor_train']}`")
    lines.append(f"  PF quarantaine: `{bs['profit_factor_quarantine']}`")
    lines.append(f"  Trades        : `{bs['n_trades_train']} + {bs['n_trades_quarantine']}`")
    lines.append(f"  MàJ : {bs['updated']}")

    if triggered_by == "manual":
        lines.append("\n_(rapport à la demande)_")

    return "\n".join(lines)


def main() -> None:
    triggered = "manual" if len(sys.argv) > 1 and sys.argv[1] == "--manual" else "auto"
    logger.info("Building daily report (triggered=%s)", triggered)
    report = build_report(triggered_by=triggered)
    print(report)
    print("\n\n---\nEnvoi sur Telegram...")
    msg_id = telegram_send(report)
    if msg_id:
        print(f"✅ Rapport publié (message_id={msg_id})")
    else:
        print("⚠️  Rapport non publié (voir logs)")


if __name__ == "__main__":
    main()
