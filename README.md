# HouseMonitor

Envoie chaque matin sur Telegram un résumé des dernières 24 h de **toutes** les entités Home Assistant.
Python 3 seulement, aucune dépendance. Prévu pour tourner sur le même Raspberry Pi que Home Assistant
(adresse `http://127.0.0.1:8123`).

> Fonctionne avec Home Assistant **Container / Supervised / Core** sur Raspberry Pi OS.
> Sur Home Assistant **OS**, il n'y a pas de cron système : il faut passer par un add-on ou une automatisation.

Le script découvre les entités tout seul et les résume selon leur type :

| Type HA                              | Résumé                                              |
|--------------------------------------|-----------------------------------------------------|
| `sensor` numérique (température, …)  | `Salon : 11°C à 07h00 → 16°C à 17h00, actuel 15°C`  |
| `binary_sensor` (Arlo, mouvement, …) | `Arlo terrasse : 3 alertes (02h10, 06h45, 23h30)`   |
| `switch` / `light` / `input_boolean` | `Arrosage : 1 cycle (06h00), éteint`                |

Les entités sans activité (0 alerte, 0 cycle) ne sont pas listées pour garder le message court.

## Installation
1. Home Assistant : Profil → Sécurité → *Jetons d'accès longue durée* → créer un jeton.
2. Telegram : créer un bot avec @BotFather (token), envoyer un message au bot, puis ouvrir
   `https://api.telegram.org/bot<TOKEN>/getUpdates` pour lire `chat.id`.
3. `cp config.example.json config.json` et remplir les jetons.
   - `domains` : types d'entités inclus.
   - `exclude` : préfixes d'`entity_id` à ignorer (ex. `sensor.sun_`).
4. Tester : `python3 summary.py --dry` (affiche sans envoyer), puis `./run.sh`.
5. Cron (`crontab -e`) pour 7h30 : `30 7 * * * /home/pi/HouseMonitor/run.sh`

`run.sh` fait d'abord un `git pull` (mise à jour automatique du script, 1×/jour), puis lance `summary.py`.

## Surveillance : être alerté si le script ne tourne plus
Le Pi ne peut pas s'auto-surveiller (s'il meurt ou perd la 4G, il ne peut rien envoyer).
On utilise donc un *dead man's switch* externe : [Healthchecks.io](https://healthchecks.io) (gratuit).

1. Créer un compte, puis un check **"HouseMonitor résumé"** : période *1 day*, grâce *1 hour*.
2. Copier son URL de ping (`https://hc-ping.com/xxxxxxxx-...`) dans `config.json` → `healthchecks_url`.
3. Onglet **Integrations** : ajouter **Email** et/ou **Telegram** (leur bot @HealthchecksBot, 2 clics).
   Pour recevoir l'alerte via *votre* bot : intégration **Webhook**, méthode GET, URL
   `https://api.telegram.org/bot<TOKEN>/sendMessage?chat_id=<CHAT_ID>&text=⚠️ HouseMonitor : $NAME est $STATUS`

Comportement :
- run OK → ping → Healthchecks attend le prochain run.
- run en erreur (HA injoignable, Telegram KO…) → ping `/fail` → **alerte immédiate**.
- Pi éteint / plus de 4G / cron cassé → aucun ping → **alerte après période + grâce** (≈ 8h30 le matin).

Optionnel, pour détecter une panne du Pi en moins de 24h : un second check "Pi alive" (période 1h)
et une ligne cron `0 * * * * curl -fsS -m 10 --retry 3 -o /dev/null https://hc-ping.com/<uuid2>`.
Coût : ~6 Ko × 24 = ~150 Ko/jour, ~4,5 Mo/mois. À doser selon votre forfait 4G.

## Consommation data (4G)
Les appels à Home Assistant restent sur `127.0.0.1` : **zéro** trafic internet.
Seuls le `git pull` et l'envoi Telegram sortent sur la 4G. Chaque exécution ajoute une ligne à `netlog.csv` :

```
date,git_pull_octets,telegram_octets,total_octets
2026-10-12 07:30,2140,8930,11070
```

- `telegram_octets` : taille réelle requête + réponse, plus ~6 Ko estimés de handshake TLS.
- `telegram_octets` inclut aussi le ping Healthchecks (~6 Ko).
- `git_pull_octets` : delta de `/proc/net/dev` sur l'interface par défaut pendant le pull
  (approximatif : inclut ce que HA envoie pendant ces quelques secondes).
  Forcer l'interface avec `IFACE=wwan0 ./run.sh` si besoin.

Le message Telegram se termine par le cumul du mois (`📡 Data 4G du script ce mois : 310 Ko`).
Ordre de grandeur attendu : ~10 à 15 Ko par jour, soit < 0,5 Mo par mois.
