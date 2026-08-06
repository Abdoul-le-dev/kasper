# Installation systemd — services et timers

## Prérequis

- Le dépôt `xauusd_mvp` est dans `/home/ubuntu/kasper/xauusd_mvp`
- Le venv Python est dans `/home/ubuntu/kasper/xauusd_mvp/venv`
- Le fichier `.env` est rempli
- Le bot Telegram a été configuré via @BotFather avec `/setprivacy` → Disable
- Le bot est ajouté comme admin du groupe Telegram

## Installation

Depuis le dossier `systemd/` du dépôt :

```bash
cd /home/ubuntu/kasper/xauusd_mvp/systemd

# Copier tous les fichiers vers /etc/systemd/system
sudo cp xauusd-scalp.service /etc/systemd/system/
sudo cp xauusd-canal.service /etc/systemd/system/
sudo cp xauusd-daily-report.service /etc/systemd/system/
sudo cp xauusd-daily-report.timer /etc/systemd/system/
sudo cp xauusd-weekly-report.service /etc/systemd/system/
sudo cp xauusd-weekly-report.timer /etc/systemd/system/

# Recharger systemd
sudo systemctl daemon-reload

# Activer et démarrer les services long-running
sudo systemctl enable --now xauusd-scalp.service
sudo systemctl enable --now xauusd-canal.service

# Activer les timers (pas les services one-shot eux-mêmes)
sudo systemctl enable --now xauusd-daily-report.timer
sudo systemctl enable --now xauusd-weekly-report.timer
```

## Vérification

```bash
# Voir l'état des services long-running
sudo systemctl status xauusd-scalp
sudo systemctl status xauusd-canal

# Voir la prochaine exécution des timers
sudo systemctl list-timers | grep xauusd

# Voir les logs en temps réel
sudo journalctl -u xauusd-scalp -f
sudo journalctl -u xauusd-canal -f
```

## Commandes utiles

```bash
# Redémarrer le scalp
sudo systemctl restart xauusd-scalp

# Stopper le scalp temporairement (ex: pour maintenance)
sudo systemctl stop xauusd-scalp

# Voir les logs des 100 dernières lignes
sudo journalctl -u xauusd-scalp -n 100

# Déclencher manuellement un rapport quotidien maintenant
sudo systemctl start xauusd-daily-report

# Déclencher manuellement le bilan hebdo + flat
sudo systemctl start xauusd-weekly-report
```

## Auto-restart

Chaque service critique (`scalp` et `canal`) est configuré avec :
- `Restart=always` — redémarrage automatique si crash
- `RestartSec=15` — attente 15s avant relance
- `StartLimitBurst=5` — max 5 tentatives
- `StartLimitIntervalSec=300` — sur 5 minutes

Si un service crashe 5 fois en 5 minutes, systemd arrête d'essayer.
Il faut alors regarder les logs et corriger avant de relancer manuellement :

```bash
sudo systemctl reset-failed xauusd-scalp
sudo systemctl start xauusd-scalp
```

## Arrêt total du système

```bash
sudo systemctl stop xauusd-scalp xauusd-canal
sudo systemctl stop xauusd-daily-report.timer xauusd-weekly-report.timer
sudo systemctl disable xauusd-scalp xauusd-canal
sudo systemctl disable xauusd-daily-report.timer xauusd-weekly-report.timer
```
