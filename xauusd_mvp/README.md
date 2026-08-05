# XAUUSD MVP — Système de scalping

**État : Phase 2 livrée.** Indicateurs, stratégies et moteur de backtest opérationnels.

## Ce qui est nouveau en Phase 2

### 6 indicateurs codés à la main (numpy pur, compat Python 3.14)

Chacun avec son fichier Pine de référence à côté :

| Fichier | Indicateur | Source Pine |
|---|---|---|
| `src/indicators/atr.py` | ATR Wilder | `atr.pine` |
| `src/indicators/supertrend.py` | SuperTrend | `supertrend.pine` |
| `src/indicators/hull_ma.py` | Hull MA | `hull_ma.pine` |
| `src/indicators/vwap.py` | Session VWAP | `vwap.pine` |
| `src/indicators/adx.py` | ADX/DMI | `adx.pine` |
| `src/indicators/ttm_squeeze.py` | TTM Squeeze | `ttm_squeeze.pine` |

**Chaque indicateur a un test look-ahead** : tronquer les données de N pas ne
change pas les valeurs antérieures. Aucun indicateur ne triche avec le futur.

### 3 stratégies candidates

| Fichier | Nom | Logique |
|---|---|---|
| `src/strategies/supertrend_atr.py` | **supertrend_atr** | Flip SuperTrend M5 + filtre ATR H1 |
| `src/strategies/hull_vwap.py` | **hull_vwap** | Cassure VWAP + Hull MA (mean-reversion) |
| `src/strategies/ttm_adx.py` | **ttm_adx** | Sortie de squeeze + ADX (breakout) |

### Moteur de backtest event-driven

`src/backtest.py` — event-driven, sur M5, avec :
- Une position à la fois (spec MVP)
- Spread historique Dukascopy appliqué à chaque entrée
- Commission XM (7 $ / lot A/R) + slippage 1 tick
- Sizing : risque fixe 10 $ / trade → 5 pertes = kill switch atteint
- **Kill switch dur 50 $/jour** : plus aucune entrée si perte cumulée ≥ 50 $
- R:R minimum 1.5, sinon skip
- Convention conservatrice : si SL et TP touchés dans la même M5, SL gagne

### Rapport HTML

`src/report.py` — rapport autoportant (aucune dépendance JS externe) :
- Verdict automatique GO / WARN / NO-GO selon profit factor
- Métriques cartes : PF, DD, win rate, avg win/loss, durée
- Répartition par session
- Courbe d'equity (canvas natif)
- Table de tous les trades

## Utilisation Phase 2

Les données doivent avoir été téléchargées en Phase 1 (`python -m src.data_collector`).

```bash
# Lancer les 3 backtests sur le train set (30 jours)
python -m src.backtest --strategy supertrend_atr
python -m src.backtest --strategy hull_vwap
python -m src.backtest --strategy ttm_adx

# Sorties générées dans reports/ :
#   backtest_supertrend_atr_train.html
#   backtest_hull_vwap_train.html
#   backtest_ttm_adx_train.html
#   + les .json équivalents
```

**Override de paramètres** (utile pour l'optimisation manuelle rapide) :

```bash
python -m src.backtest --strategy supertrend_atr --params st_length=14 st_factor=2.5 tp_k=3.0
```

## Comment interpréter les rapports

Ouvrir les 3 fichiers HTML dans un navigateur. Regarder :

1. **Le bandeau verdict** (vert / jaune / rouge)
2. **Profit factor** :
   - PF ≥ 1.5 → GO
   - 1.0 ≤ PF < 1.5 → WARN, à confirmer par walk-forward
   - PF < 1.0 → NO-GO
3. **Nombre de trades** : < 20 = échantillon trop petit, méfiance
4. **Max drawdown** : < 20 % du PnL total = acceptable
5. **Répartition par session** : la stratégie doit générer des trades dans
   plusieurs sessions, pas concentrer sur une seule

## Ce que je te demande maintenant (Checkpoint 2)

Envoie-moi les **3 résumés console** (le bloc `======` à la fin de chaque backtest).
Optionnel : les 3 fichiers HTML pour inspection détaillée.

Sur la base des résultats, je décide (avec ta validation) :

- **Cas A** : au moins une stratégie a PF ≥ 1.5 → on garde la meilleure, on passe à Phase 3
- **Cas B** : toutes en WARN (1.0 ≤ PF < 1.5) → on garde la meilleure, on optimise, on reste prudent
- **Cas C** : toutes en NO-GO → Option A oblige à garder la meilleure quand même, mais on **restera bloqué en paper trading** jusqu'à discussion

## Tests unitaires

```bash
python -m pytest tests/ -v
```

Attendu : **25 tests verts** (6 data_collector + 15 indicateurs + 4 backtest smoke).

## Installation (rappel)

```bash
# Sur ton VPS, tu écrases les fichiers Phase 1 par ceux de Phase 2
cd /home/ubuntu/kasper/xauusd_mvp
unzip -o xauusd_mvp_phase2.zip

# Les nouveaux fichiers apparaissent dans src/indicators/ et src/strategies/
# et src/backtest.py + src/report.py sont ajoutés.
# Aucune dépendance nouvelle à installer.

# Vérifier
python -m pytest tests/ -v
# → 25 passed
```
