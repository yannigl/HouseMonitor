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
   - `healthchecks` : URL de ping + cadence (voir plus bas).
   - `listener` : réglages du listener Telegram (voir plus bas).
   - `domains` : types d'entités inclus.
   - `exclude` : préfixes d'`entity_id` à ignorer (ex. `sensor.sun_`).
4. Tester : `python3 summary.py --dry` (affiche sans envoyer), puis `./run.sh`.
5. Cron (`crontab -e`) pour 7h30 : `30 7 * * * /home/pi/HouseMonitor/run.sh`
   Vérifier qu'il n'y a qu'**une** ligne : `crontab -l`. Si elle est en double :
   `crontab -l | awk '!seen[$0]++' | crontab -` (`run.sh` ignore de toute façon un second run simultané).
6. Listener : voir *Commandes Telegram* ci-dessous (service systemd, pas un cron).

`run.sh` fait d'abord un `git pull` (mise à jour automatique du script, 1×/jour), puis lance `summary.py`.

## Qui tourne comment
| Service | Mécanisme | Lancement |
|---|---|---|
| résumé du matin (`summary.py`) | cron, 1×/jour | `30 7 * * * /home/pi/HouseMonitor/run.sh` |
| listener (`listener.py`) | service systemd permanent | `sudo systemctl enable --now housemonitor-listener` |
| ping Healthchecks | dans la boucle du listener (4 h) + fin du résumé | rien à lancer |

## Commandes Telegram (listener)
`listener.py` tourne en permanence (service systemd) et écoute vos commandes sur le même bot :

| Commande | Effet |
|---|---|
| `/help` | Liste des commandes (aussi dans le menu « / » de Telegram) |
| `/status` | État des 3 services + data 4G |
| `/data` | Data 4G depuis le dernier reset : part du script et total du Pi (voir *Compteur de data du Pi*) |
| `/data reset` | Remet le compteur à 0 ; remise à zéro automatique le même jour du mois suivant |
| `/list` | Tous les appareils HA (nom + `entity_id`) groupés par domaine |
| `/update` | Le résumé du matin, tout de suite |
| `/update all` | État actuel de **toutes** les entités avec l'heure du dernier changement |
| `/stop ping` / `/start ping` | Coupe / relance le heartbeat Healthchecks (⚠️ couper = alerte après la grâce) |
| `/stop monitor` / `/start monitor` | Coupe / relance le résumé du matin (le cron tourne, mais n'envoie rien) |
| `/shutdown` (alias `/stop pi`) | Éteint le Pi après confirmation par `/shutdown confirm` (voir *Éteindre le Pi à distance*) |
| `/stop listener` / `/start listener` | Mode éco (1 appel toutes les `listener.slow_poll_hours`, 24 h) / mode réactif |

Les réponses de plus de 4 000 caractères (limite Telegram) arrivent en pièce jointe `.txt`, en un seul
message. Les réponses `/stop`, `/start` et `/status` se terminent par le statut des trois services.
L'état est dans `state.json`.
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

### Éteindre le Pi à distance (`/shutdown`)
Pour une coupure de courant prévue. Installation, **une fois** : `sudo ./setup-power.sh` (règle `sudoers`
limitée à la seule commande `systemctl poweroff`).

```
vous    : /shutdown
bot     : ⚠️ Éteindre le Pi ?  • signal Healthchecks : /fail …  Confirmez dans les 60 s : /shutdown confirm
vous    : /shutdown confirm
bot     : ⏻ Extinction dans 5 s. …            (acquittement)
```
Ensuite, dans l'ordre : acquittement de la commande chez Telegram, signal à Healthchecks, enregistrement des
compteurs de data, extinction. Si l'extinction échoue, le `/fail` est annulé par un ping OK et l'erreur vous est envoyée.

Garde-fous :
- **Confirmation obligatoire** (60 s) : `/stop pi` est à une lettre de `/stop ping`, une faute de frappe ne doit pas
  éteindre une machine que l'on ne peut rallumer qu'en la rebranchant.
- La demande en attente n'existe qu'en mémoire : si Telegram rejouait `/shutdown confirm` au redémarrage, elle serait
  ignorée (le Pi n'a pas d'horloge à pile, on ne peut pas se fier à l'âge du message). La commande est en outre
  acquittée chez Telegram avant l'extinction.
- Sans la règle `sudoers`, `/shutdown` refuse **avant** d'envoyer quoi que ce soit.

Signal envoyé à Healthchecks, réglable dans `config.json` → `shutdown.healthchecks` :

| Valeur | Effet | À choisir si |
|---|---|---|
| `"fail"` (défaut) | Alerte DOWN immédiate (mail + Telegram), puis alerte UP au redémarrage avec la durée d'arrêt | vous voulez une confirmation et la durée exacte de la coupure |
| `"log"` | Note « arrêt volontaire » dans le journal du check, **sans alerte** ; si le Pi revient avant période + grâce (5 h), aucune alerte, sinon DOWN | vous ne voulez pas d'alerte pour un arrêt voulu |
| `"none"` | Rien | — |

Mettre le check en *pause* (API Healthchecks, clé d'API requise) n'est pas proposé : un check en pause n'alerte
plus, donc si le Pi ne redémarre pas après la coupure, personne ne le saurait.

Le Pi **ne redémarre pas tout seul** après un arrêt : il repart quand le courant est coupé puis remis (ou en le
débranchant/rebranchant). Home Assistant (Docker, `unless-stopped`) revient avec lui. Ne faites pas `docker stop`
avant : un conteneur arrêté à la main n'est pas relancé au démarrage avec cette politique.

### Coût data du listener
Pas de webhook possible derrière la 4G : le Pi interroge Telegram (`getUpdates`) en **long polling sur
une connexion persistante**. Le handshake TLS (~6 Ko) n'est payé qu'à la connexion, ensuite chaque appel
vide coûte ~0,6 Ko. Réglable via `listener.poll_timeout` (secondes) dans `config.json` :

| `poll_timeout` | appels / jour | data / mois | réactivité |
|---|---|---|---|
| 60 | 1 440 | ~26 Mo | immédiate |
| 300 (défaut) | 288 | ~5 Mo | immédiate |
| 600 | 144 | ~2,6 Mo | immédiate, mais le NAT 4G peut couper (= reconnexion, 6 Ko) |
| `/stop listener` | 1 | ~0,2 Mo | jusqu'à 24 h (le ping Healthchecks continue toutes les 4 h) |

## Surveillance : être alerté si le script ne tourne plus
Le Pi ne peut pas s'auto-surveiller (s'il meurt ou perd la 4G, il ne peut rien envoyer).
On utilise donc un *dead man's switch* externe : [Healthchecks.io](https://healthchecks.io) (gratuit).

### Créer le check
*Add Check* → **Name** `MyHouseMonitor`, **Schedule** Simple, **Period** `4 hours`, **Grace** `1 hour`.
Ces deux valeurs doivent correspondre à `healthchecks.period_hours` / `grace_hours` dans `config.json`
(le script ne les envoie pas à Healthchecks, il s'en sert pour afficher "alerte après X h" dans `/status`).

**Tags** : facultatif, ce sont juste des étiquettes pour filtrer le tableau de bord quand on a plusieurs
checks (ex. `pi maison`). Avec un seul check, laissez vide.

Une fois créé, cliquez sur le check : l'URL de ping (`https://hc-ping.com/xxxxxxxx-…`) est affichée en haut.
Copiez-la dans `config.json` → `healthchecks.url`. Vous pouvez supprimer "My First Check".

### Être alerté
Onglet **Integrations** (menu du haut) :
- **Email** : déjà présent sur l'adresse du compte, vérifiez qu'il est bien associé au check.
- **Telegram** : *Add Integration* → Telegram → suivre le lien vers leur bot @HealthchecksBot → *Start*.
  C'est un autre bot que le vôtre : si votre token casse, l'alerte arrive quand même.
- Variante avec *votre* bot : intégration **Webhook**, méthode GET, URL
  `https://api.telegram.org/bot<TOKEN>/sendMessage?chat_id=<CHAT_ID>&text=⚠️ HouseMonitor : $NAME est $STATUS`

Conseil : Email **+** Telegram, deux canaux indépendants pour zéro coût.

### Qui pinge et quand
Le listener pinge toutes les `healthchecks.ping_interval_hours` (4 h, ~6 Ko/ping ≈ 1 Mo/mois),
et le résumé du matin pinge aussi. Avec période 4 h + grâce 1 h, une panne est signalée en 5 h max.

Comportement :
- run OK → ping → Healthchecks attend le prochain run.
- run en erreur (HA injoignable, Telegram KO…) → ping `/fail` → **alerte immédiate**.
- Pi éteint / plus de 4G / listener mort → aucun ping → **alerte après période + grâce** (≤ 4h30).

## Dépannage : DNS (« Could not resolve host », « Temporary failure in name resolution »)
```bash
python3 dnscheck.py
```
Montre les routes IPv4/IPv6 du Pi, ce que résout le système (5 essais par nom : on voit si les échecs sont
intermittents) et ce que répond chaque serveur DNS interrogé directement (ceux de `/etc/resolv.conf`, plus
Cloudflare et Quad9). Si les serveurs publics répondent et pas celui du routeur, le DNS du routeur est en cause.

Le résumé, le listener et les pings résistent mieux qu'un `ping` ou un `git pull` lancés à la main : le listener garde
une connexion ouverte avec Telegram (aucune résolution tant qu'elle tient), réessaie en boucle, et l'envoi du
résumé et le `git pull` de `run.sh` font 3 essais. Une panne DNS intermittente passe donc inaperçue côté script.

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

Le message Telegram se termine par le cumul du cycle (`📡 Data 4G : 310 Ko depuis le 13/10 (remise à zéro auto le 13/11)`).

**Cycle de comptage** : par défaut du 1er au 1er du mois. `/data reset` fait démarrer un nouveau cycle
maintenant ; il se remet à zéro seul à minuit le même jour du mois suivant (calez-le sur le jour de
renouvellement de votre forfait 4G). Si ce jour n'existe pas (31 → février), c'est le dernier jour du mois.
`netlog.csv` n'est jamais effacé, seul le point de départ du cycle (`data_cycle.json`) change.
Ordre de grandeur attendu : ~15 Ko/jour pour le résumé, ~170 Ko/jour pour le listener (réglage par défaut).

### Compteur de data du Pi (internet seulement)
Les chiffres ci-dessus ne concernent que le script. Pour connaître le total du Pi (Home Assistant, ses
intégrations cloud comme Arlo, mises à jour système…), installez le compteur, **une seule fois** :
```bash
sudo ./setup-netcount.sh
```
Il crée des compteurs `nftables` qui ne comptent que les échanges avec des adresses **non locales**,
donc ce qui passe réellement par le routeur 4G : le trafic local avec vos appareils (Wi-Fi, Zigbee,
MQTT…) n'est pas compté. Il est relancé à chaque démarrage par systemd, et une règle `sudoers` limitée à la
seule lecture de ces compteurs permet au listener de les lire sans mot de passe.

`/data` et le message du matin affichent alors :
```
📡 Data 4G depuis le 13/10 (remise à zéro auto le 13/11)
• Script HouseMonitor : 312 Ko
• Pi au total : 410 Mo
• dont HA, système… (hors script) : 409,7 Mo
```
Sans le compteur, la ligne « Pi au total » indique « non mesuré » ; le reste fonctionne.

À savoir :
- Les compteurs du noyau repartent à 0 au reboot : le listener les lit toutes les heures et cumule, il ne
  perd donc au pire que l'heure précédant un arrêt brutal (un arrêt propre ou un `reboot` enregistre tout).
- Le trafic compté est celui vu par le Pi, en-têtes IP/TCP inclus ; le routeur 4G ajoute un peu de son côté.
  Pour la référence « forfait », comparez au compteur de votre routeur.
- L'IPv6 : le préfixe du réseau local est exclu au chargement. Si le préfixe change durablement,
  relancez `sudo ./setup-netcount.sh` (cela remet les compteurs du noyau à 0, sans effet sur le cumul).
- Désinstaller : `sudo systemctl disable --now housemonitor-netcount && sudo rm /etc/systemd/system/housemonitor-netcount.service /etc/sudoers.d/housemonitor-netcount /usr/local/sbin/housemonitor-netcount && sudo nft delete table inet housemonitor`
- Le `git pull` du matin est mesuré avec ce compteur quand il est installé (plus de trafic local mélangé).
