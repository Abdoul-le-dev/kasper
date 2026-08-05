"""
report.py

Rapport HTML simple et autoportant (aucune dépendance à un serveur ou lib front).
Ouvrable dans n'importe quel navigateur.
"""

from __future__ import annotations
import json
from dataclasses import asdict
from pathlib import Path


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8"/>
<title>Backtest — {name}</title>
<style>
  body {{ font-family: -apple-system, Segoe UI, Roboto, sans-serif; margin: 30px; color: #222; }}
  h1 {{ border-bottom: 2px solid #333; padding-bottom: 8px; }}
  .verdict {{ padding: 15px; border-radius: 8px; margin: 20px 0; font-size: 18px; font-weight: bold; }}
  .verdict.go   {{ background: #d4edda; color: #155724; border: 1px solid #c3e6cb; }}
  .verdict.warn {{ background: #fff3cd; color: #856404; border: 1px solid #ffeaa7; }}
  .verdict.nogo {{ background: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; }}
  .metrics {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 15px; margin: 20px 0; }}
  .card {{ background: #f8f9fa; padding: 15px; border-radius: 6px; }}
  .card .label {{ font-size: 12px; text-transform: uppercase; color: #666; }}
  .card .value {{ font-size: 24px; font-weight: bold; margin-top: 5px; }}
  table {{ border-collapse: collapse; width: 100%; margin: 20px 0; font-size: 13px; }}
  th, td {{ padding: 6px 10px; border-bottom: 1px solid #ddd; text-align: left; }}
  th {{ background: #f0f0f0; }}
  .win {{ color: #155724; }}
  .loss {{ color: #721c24; }}
  pre {{ background: #f0f0f0; padding: 12px; border-radius: 4px; font-size: 12px; overflow-x: auto; }}
  canvas {{ margin: 20px 0; }}
</style>
</head>
<body>
<h1>Backtest — {name}</h1>
<div class="verdict {verdict_class}">{verdict_text}</div>

<h2>Métriques</h2>
<div class="metrics">
  <div class="card"><div class="label">Trades</div><div class="value">{n_trades}</div></div>
  <div class="card"><div class="label">Win Rate</div><div class="value">{win_rate:.1%}</div></div>
  <div class="card"><div class="label">Profit Factor</div><div class="value">{pf}</div></div>
  <div class="card"><div class="label">Total P&amp;L</div><div class="value">{pnl} $</div></div>
  <div class="card"><div class="label">Max Drawdown</div><div class="value">{dd} $</div></div>
  <div class="card"><div class="label">Avg Win</div><div class="value">{avg_win} $</div></div>
  <div class="card"><div class="label">Avg Loss</div><div class="value">{avg_loss} $</div></div>
  <div class="card"><div class="label">Avg Duration</div><div class="value">{duration} min</div></div>
</div>

<h2>Répartition par session</h2>
<pre>{by_session}</pre>

<h2>Paramètres de la stratégie</h2>
<pre>{params}</pre>

<h2>Courbe d'equity</h2>
<canvas id="eq" width="900" height="300" style="border:1px solid #ccc"></canvas>

<h2>Trades ({n_trades})</h2>
<table>
<thead><tr><th>#</th><th>Entry</th><th>Exit</th><th>Dir</th><th>Session</th>
<th>Entry $</th><th>SL $</th><th>TP $</th><th>Lot</th><th>Reason</th><th>PnL $</th></tr></thead>
<tbody>{trades_rows}</tbody>
</table>

<script>
const eqData = {equity_curve_json};
const c = document.getElementById('eq');
const ctx = c.getContext('2d');
if (eqData.length > 0) {{
  const vals = eqData.map(x => x[1]);
  const min = Math.min(0, ...vals);
  const max = Math.max(0, ...vals);
  const range = (max - min) || 1;
  const W = c.width, H = c.height, pad = 30;
  ctx.strokeStyle = '#ccc';
  ctx.beginPath(); ctx.moveTo(pad, H - pad); ctx.lineTo(W - pad, H - pad); ctx.stroke();
  ctx.beginPath(); ctx.moveTo(pad, pad); ctx.lineTo(pad, H - pad); ctx.stroke();
  // ligne zéro
  const zeroY = H - pad - ((0 - min) / range) * (H - 2 * pad);
  ctx.strokeStyle = '#888'; ctx.setLineDash([3,3]);
  ctx.beginPath(); ctx.moveTo(pad, zeroY); ctx.lineTo(W - pad, zeroY); ctx.stroke();
  ctx.setLineDash([]);
  // courbe
  ctx.strokeStyle = '#0066cc'; ctx.lineWidth = 2;
  ctx.beginPath();
  eqData.forEach((pt, i) => {{
    const x = pad + (i / (eqData.length - 1 || 1)) * (W - 2 * pad);
    const y = H - pad - ((pt[1] - min) / range) * (H - 2 * pad);
    if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  }});
  ctx.stroke();
  ctx.fillStyle = '#333'; ctx.font = '12px sans-serif';
  ctx.fillText(max.toFixed(1) + ' $', 2, pad + 4);
  ctx.fillText(min.toFixed(1) + ' $', 2, H - pad + 4);
}}
</script>
</body>
</html>
"""


def _verdict(result) -> tuple[str, str]:
    pf = result.profit_factor
    if pf == -1.0:  # infini (aucune perte)
        return "go", "✅ GO — Aucune perte enregistrée (attention: sample peut être trop petit)"
    if pf >= 1.5:
        return "go", f"✅ GO — Profit factor {pf} ≥ 1.5"
    if pf >= 1.0:
        return "warn", f"⚠️ WARN — Profit factor {pf} entre 1.0 et 1.5. Marginal. À valider en walk-forward."
    return "nogo", f"❌ NO-GO — Profit factor {pf} < 1.0. Stratégie non retenue."


def render_report(result, output_path: Path) -> None:
    """Écrit un rapport HTML complet."""
    verdict_class, verdict_text = _verdict(result)

    trades_rows = ""
    for i, t in enumerate(result.trades, 1):
        cls = "win" if t["pnl"] > 0 else "loss"
        trades_rows += (
            f"<tr class='{cls}'><td>{i}</td>"
            f"<td>{t['entry_ts']}</td><td>{t['exit_ts']}</td>"
            f"<td>{t['direction']}</td><td>{t['session']}</td>"
            f"<td>{t['entry']}</td><td>{t['sl']}</td><td>{t['tp']}</td>"
            f"<td>{t['lot']}</td><td>{t['reason']}</td><td>{t['pnl']:+.2f}</td></tr>"
        )

    html = HTML_TEMPLATE.format(
        name=result.strategy_name,
        verdict_class=verdict_class,
        verdict_text=verdict_text,
        n_trades=result.n_trades,
        win_rate=result.win_rate,
        pf=result.profit_factor,
        pnl=result.total_pnl_usd,
        dd=result.max_drawdown_usd,
        avg_win=result.avg_win_usd,
        avg_loss=result.avg_loss_usd,
        duration=result.avg_trade_duration_min,
        by_session=json.dumps(result.trades_by_session, indent=2),
        params=json.dumps(result.params, indent=2),
        equity_curve_json=json.dumps(result.equity_curve),
        trades_rows=trades_rows,
    )
    output_path.write_text(html, encoding="utf-8")
