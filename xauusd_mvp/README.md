# XAUUSD MVP — Système de scalping

Système déterministe de scalping sur XAUUSD, sessions Londres/NY/overlap.
Une stratégie retenue parmi 3 candidates, testée sur 10 mois d'historique
Dukascopy, validée sur 2 mois de quarantaine, puis paper trading sur compte
démo XM via MetaApi.

**État actuel : Phase 1 (Fondations) livrée.**

## Décisions figées

| Point | Valeur |
|---|---|
| Actif | XAUUSD |
| Sessions tradées | Londres, NY, overlap |
| Timeframes | M1/M5/M15 décision, M30/H1 contexte |
| Historique | 12 mois Dukascopy, 2 derniers mois en quarantaine |
| Perte max quotidienne | 50 $ (kill switch dur) |
| Positions simultanées | 1 max, pas de pyramiding |
| R:R minimum | 1.5 |
| Live | interdit tant que `LIVE_ENABLED=true` n'est pas basculé manuellement |

## Installation (VPS Ubuntu 24.04)

```bash
# 1) Cloner ou dézipper dans /home/ubuntu/xauusd_mvp
cd /home/ubuntu/xauusd_mvp

# 2) Créer un venv (recommandé)
python3 -m venv .venv
source .venv/bin/activate

# 3) Installer les dépendances
pip install -r requirements.txt

# 4) Configurer les credentials
cp .env.example .env
nano .env   # remplir METAAPI_TOKEN, METAAPI_ACCOUNT_ID, TELEGRAM_*
```

## Phase 1 — Récupération des données Dukascopy

```bash
# Vérifier la config
python config.py

# Lancer le téléchargement + agrégation
python -m src.data_collector
```

Ce que ça fait :

1. Télécharge les ticks XAUUSD sur 12 mois (weekends exclus).
2. Cache local dans `data/_dukascopy_cache/` — idempotent, resume automatique.
3. Agrège en OHLC M1 + tick volume + spread moyen.
4. Sépare les 2 derniers mois en `data/quarantine/` (INTOUCHABLE jusqu'à validation finale).
5. Génère les Parquet M1/M5/M15/M30/H1 dans `data/` et `data/quarantine/`.
6. Écrit un rapport qualité dans `reports/data_quality.json`.

Durée estimée : **1 à 4 heures** selon bande passante (~8 000 fichiers à télécharger).
Volume disque : ~500 Mo pour le cache bi5, ~200 Mo pour les Parquet.

## Vérification post-Phase 1

```bash
# Lire le rapport qualité
cat reports/data_quality.json

# Compter les bougies dans chaque Parquet
python -c "
import polars as pl
from pathlib import Path
for f in sorted(Path('data').glob('*.parquet')):
    df = pl.read_parquet(f)
    print(f'{f.name:25s} {df.height:>8d} bars   ({df[\"ts\"].min()} → {df[\"ts\"].max()})')
for f in sorted(Path('data/quarantine').glob('*.parquet')):
    df = pl.read_parquet(f)
    print(f'QUAR {f.name:20s} {df.height:>8d} bars   ({df[\"ts\"].min()} → {df[\"ts\"].max()})')
"
```

Attendus indicatifs (approximatifs, dépend des jours fériés) :

- M1 train : ~350 000 bars
- M5 train : ~70 000 bars
- H1 train : ~5 800 bars
- Quarantaine M5 : ~15 000 bars

Le rapport qualité `reports/data_quality.json` doit montrer :
- `incoherent_ohlc: 0`
- `duplicates_ts: 0`
- `small_gaps_5_2000_min` < 1% du total (petits gaps intraday, normaux)
- `big_gaps_weekends` ≈ nombre de weekends dans la fenêtre (~50)

## Tests unitaires

```bash
python -m pytest tests/ -v
```

## Prochaines phases

- **Phase 2** — Reproduction des 3 candidates + backtest engine + rapport HTML
- **Phase 3** — Optimisation (grid search restreint) + élimination
- **Phase 4** — Stress tests (7 niveaux) + validation quarantaine
- **Phase 5** — Paper trading MetaApi démo (≥ 24h)
- **Phase 6** — Bascule live (décision manuelle)

Chaque phase produit un livrable zippé. Aucune ne démarre sans validation
explicite de la précédente.

## Architecture livrée en Phase 1

```
xauusd_mvp/
├── config.py                  # constantes globales
├── .env.example
├── requirements.txt
├── src/
│   ├── data_collector.py      # NEW — Dukascopy → Parquet
│   ├── session_context.py     # REUSED
│   ├── risk_manager.py        # ADAPTÉ (kill switch 50$)
│   ├── journal.py             # REUSED
│   ├── metaapi_connector.py   # REUSED (utilisé en Phase 5)
│   └── telegram_notifier.py   # REUSED (utilisé en Phase 5)
├── tests/
│   └── test_data_collector.py
├── data/                      # sortie du data_collector
│   └── quarantine/            # 2 derniers mois isolés
├── reports/                   # rapports auto-générés
└── logs/
```

## Règles absolues

1. Ne JAMAIS toucher aux fichiers dans `data/quarantine/` avant l'étape de
   validation finale (Phase 4). L'intégrité de la quarantaine est le seul
   filet anti-overfit du projet.
2. Ne JAMAIS mettre `LIVE_ENABLED=true` avant qu'une stratégie ait passé
   les Phases 2-5. Le kill switch 50 $ est le dernier garde-fou, pas le premier.
3. Ne JAMAIS ré-optimiser les paramètres après la Phase 3 sans re-jouer
   les Phases 4-5 dans leur intégralité.
