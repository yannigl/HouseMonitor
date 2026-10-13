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

## Commandes Telegram (listener)
`listener.py` tourne en permanence (service systemd) et écoute vos commandes sur le même bot :

| Commande | Effet |
|---|---|
| `/status` | État des 3 services + data 4G du mois |
| `/stop ping` / `/start ping` | Coupe / relance le heartbeat Healthchecks (⚠️ couper = alerte après la grâce) |
| `/stop monitor` / `/start monitor` | Coupe / relance le résumé du matin (le cron tourne, mais n'envoie rien) |
| `/stop listener` / `/start listener` | Mode éco (1 appel toutes les `slow_poll_hours` h) / mode réactif |

Chaque réponse se termine par le statut des trois services. L'état est dans `state.json`.
Seuls les messages venant de votre `telegram_chat_id` sont pris en compte.

Installation du service :
```bash
sudo cp housemonitor-listener.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now housemonitor-listener
systemctl status housemonitor-listener      # doit être "active (running)"
journalctl -u housemonitor-listener -f       # logs en direct
```
(Adaptez `User=` et les chemins dans le `.service` si vous n'êtes pas l'utilisateur `pi`.)
Le listener se relance tout seul quand le `git pull` du matin modifie `listener.py`.

### Coût data du listener
Pas de webhook possible derrière la 4G : le Pi interroge Telegram (`getUpdates`) en **long polling sur
une connexion persistante**. Le handshake TLS (~6 Ko) n'est payé qu'à la connexion, ensuite chaque appel
vide coûte ~0,6 Ko. Réglable via `poll_timeout` (secondes) dans `config.json` :

| `poll_timeout` | appels / jour | data / mois | réactivité |
|---|---|---|---|
| 60 | 1 440 | ~26 Mo | immédiate |
| 300 (défaut) | 288 | ~5 Mo | immédiate |
| 600 | 144 | ~2,6 Mo | immédiate, mais le NAT 4G peut couper (= reconnexion, 6 Ko) |
| `/stop listener` | 6 | ~1 Mo | jusqu'à 4 h |

## Surveillance : être alerté si le script ne tourne plus
Le Pi ne peut pas s'auto-surveiller (s'il meurt ou perd la 4G, il ne peut rien envoyer).
On utilise donc un *dead man's switch* externe : [Healthchecks.io](https://healthchecks.io) (gratuit).

1. Créer un compte, puis un check **"HouseMonitor résumé"** : période *1 day*, grâce *1 hour*.
2. Copier son URL de ping (`https://hc-ping.com/xxxxxxxx-...`) dans `config.json` → `healthchecks_url`.
3. Onglet **Integrations** : ajouter **Email** et/ou **Telegram** (leur bot @HealthchecksBot, 2 clics).
   Pour recevoir l'alerte via *votre* bot : intégration **Webhook**, méthode GET, URL
   `https://api.telegram.org/bot<TOKEN>/sendMessage?chat_id=<CHAT_ID>&text=⚠️ HouseMonitor : $NAME est $STATUS`

Le ping est envoyé par le listener toutes les `ping_interval_hours` (4 h par défaut, ~1 Mo/mois) **et**
par le résumé du matin. Régler le check Healthchecks sur période *4 hours*, grâce *30 min*.

Comportement :
- run OK → ping → Healthchecks attend le prochain run.
- run en erreur (HA injoignable, Telegram KO…) → ping `/fail` → **alerte immédiate**.
- Pi éteint / plus de 4G / listener mort → aucun ping → **alerte après période + grâce** (≤ 4h30).

## Consommation data (4G)
Les appels à Home Assistant restent sur `127.0.0.1` : **zéro** trafic internet.
Seuls le `git pull` et l'envoi Telegram sortent sur la 4G. Chaque exécution ajoute une ligne à `netlog.csv` :

```
date,source,git_pull_octets,telegram_octets,total_octets
2026-10-12 07:30,summary,2140,8930,11070
2026-10-13 00:00,listener,0,190412,190412
```
`source` = `summary` (une ligne par run du matin) ou `listener` (une ligne par jour, cumul des polls + pings).

- `telegram_octets` : taille réelle requête + réponse, plus ~6 Ko estimés de handshake TLS.
- `telegram_octets` inclut aussi le ping Healthchecks (~6 Ko).
- `git_pull_octets` : delta de `/proc/net/dev` sur l'interface par défaut pendant le pull
  (approximatif : inclut ce que HA envoie pendant ces quelques secondes).
  Forcer l'interface avec `IFACE=wwan0 ./run.sh` si besoin.

Le message Telegram se termine par le cumul du mois (`📡 Data 4G du script ce mois : 310 Ko`).
Ordre de grandeur attendu : ~15 Ko/jour pour le résumé, ~170 Ko/jour pour le listener (réglage par défaut).
