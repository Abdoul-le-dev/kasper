"""
weekly_report.py

Rapport hebdomadaire + FERMETURE FORCÉE de toutes les positions ouvertes.
Lancé automatiquement par systemd timer chaque vendredi à 21h UTC (avant clôture NY),
ou manuellement.

Actions :
  1. Ferme toutes les positions ouvertes sur MetaApi
  2. Compile les stats de la semaine (lundi 00:00 UTC → vendredi 21h UTC)
  3. Publie le bilan sur Telegram
"""

from __future__ import annotations

import logging
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402
from src.metaapi_connector import (
    get_account_summary, get_open_trades, close_trade,
)
from src.shared import telegram_send, journal_read, journal_append

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger("weekly_report")


def close_all_positions() -> list[dict]:
    """Ferme toutes les positions ouvertes. Retourne la liste des fermetures effectuées."""
    closed = []
    try:
        trades = get_open_trades()
    except Exception as e:
        logger.error("Cannot list trades: %s", e)
        return closed

    for t in trades:
        pos_id = t.get("id")
        if not pos_id:
            continue
        try:
            result = close_trade(pos_id)
            closed.append({"id": pos_id, "result": result})
            journal_append({
                "kind": "position_closed_weekly_flat",
                "source": "system",
                "position_id": str(pos_id),
                "exit_reason": "WEEKLY_FLAT",
            })
            logger.info("Closed %s: %s", pos_id, result)
            time.sleep(0.5)  # petit délai pour éviter rate limit
        except Exception as e:
            logger.error("Failed to close %s: %s", pos_id, e)
    return closed


def collect_week() -> dict:
    """Collecte tous les trades depuis lundi 00:00 UTC."""
    now = datetime.now(timezone.utc)
    monday = now - timedelta(days=now.weekday())
    monday_midnight = monday.replace(hour=0, minute=0, second=0, microsecond=0)

    entries = journal_read(after=monday_midnight)
    opens = [e for e in entries if e.get("kind") == "position_opened"]
    closes = [e for e in entries if e.get("kind", "").startswith("position_closed")]

    trades = []
    close_by_id = {c.get("position_id"): c for c in closes}
    for op in opens:
        pid = op.get("position_id")
        cl = close_by_id.get(pid, {})
        trades.append({
            "source": op.get("source", "?"),
            "direction": op.get("direction"),
            "pnl": cl.get("pnl_usd", 0),
            "day": (op.get("ts_utc") or "")[:10],
        })
    return {"trades": trades, "week_start": monday_midnight}


def build_report(closed_forced: list[dict]) -> str:
    now = datetime.now(timezone.utc)
    data = collect_week()
    trades = data["trades"]

    scalp = [t for t in trades if t["source"] == "scalp"]
    direct = [t for t in trades if t["source"] == "direct"]
    scalp_pnl = sum(t["pnl"] or 0 for t in scalp)
    direct_pnl = sum(t["pnl"] or 0 for t in direct)
    total_pnl = scalp_pnl + direct_pnl

    wins = [t for t in trades if (t["pnl"] or 0) > 0]
    losses = [t for t in trades if (t["pnl"] or 0) < 0]
    win_rate = (len(wins) / len(trades) * 100) if trades else 0

    # Équité actuelle vs semaine précédente : simple, on utilise l'équité actuelle
    try:
        summary = get_account_summary()
        eq_now = summary["equite"]
    except Exception:
        eq_now = None

    # Ventilation par jour
    days = {}
    for t in trades:
        d = t["day"]
        days.setdefault(d, {"n": 0, "pnl": 0.0})
        days[d]["n"] += 1
        days[d]["pnl"] += t["pnl"] or 0

    lines = []
    lines.append(f"🏁 *Bilan hebdomadaire — semaine {now.isocalendar().week}*\n")

    if eq_now is not None:
        lines.append(f"Équité fin de semaine : `{eq_now:.2f} $`")
    sign = "+" if total_pnl >= 0 else ""
    lines.append(f"P&L semaine : `{sign}{total_pnl:.2f} $`\n")

    lines.append(f"*Positions* : {len(trades)}")
    lines.append(f"  Wins  : {len(wins)}   Losses : {len(losses)}")
    lines.append(f"  Win rate : `{win_rate:.1f} %`\n")

    lines.append(f"*Flux :*")
    lines.append(f"  Scalp   : {len(scalp)} ({scalp_pnl:+.2f} $)")
    lines.append(f"  Discret : {len(direct)} ({direct_pnl:+.2f} $)\n")

    if days:
        lines.append("*Par jour :*")
        for d in sorted(days.keys()):
            info = days[d]
            lines.append(f"  {d} : {info['n']} pos, {info['pnl']:+.2f} $")
        lines.append("")

    if closed_forced:
        lines.append(f"*Positions clôturées weekend :* {len(closed_forced)}")
        lines.append("")

    lines.append("*Backtesting week strategie :*")
    bs = config.BACKTEST_WEEK_STATS
    lines.append(f"  PF train / quar. : `{bs['profit_factor_train']} / {bs['profit_factor_quarantine']}`")
    lines.append(f"  MàJ : {bs['updated']}\n")

    lines.append("_Bonne fin de semaine._")
    return "\n".join(lines)


def main() -> None:
    logger.info("Starting weekly flat + report")
    print("🏁 Fermeture forcée de toutes les positions ouvertes...")
    closed = close_all_positions()
    print(f"   {len(closed)} position(s) fermée(s).")

    print("📊 Construction du rapport hebdo...")
    report = build_report(closed)
    print(report)
    print("\n---\nEnvoi Telegram...")
    msg_id = telegram_send(report)
    if msg_id:
        print(f"✅ Rapport publié (message_id={msg_id})")


if __name__ == "__main__":
    main()
