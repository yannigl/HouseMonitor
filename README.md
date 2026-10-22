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

Le regroupement par appareil passe par le moteur de templates de Home Assistant (une requête locale vers
`/api/template`), car l'API REST ne décrit que les entités. Si cette requête échoue, `/list` retombe sur la liste à plat
et le précise ; le détail est dans `run.log`. Les entités sans appareil (helpers, automations, soleil) sont
regroupées à part, par domaine.

Après toute modification de `config.json`, vérifiez-le avant de relancer quoi que ce soit :
```bash
cd ~/HouseMonitor && python3 -c "import common; print('config OK')"
```
Un `config.json` invalide fait échouer le résumé **et** met le listener en boucle de redémarrage (systemd le relance toutes
les 30 s) : le bot ne répond plus et plus aucun ping n'est envoyé.

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
| `/list` | Les **appareils** Home Assistant, avec le nombre et le type de leurs entités (un appareil expose souvent plusieurs capteurs : batterie, signal…) |
| `/list <mot>` | Le détail des appareils dont le nom, le fabricant, le modèle, la pièce ou une entité contient `<mot>` (ex. `/list roomba`, `/list salon`) |
| `/list all` | Toutes les entités à plat, par domaine, avec leur `entity_id` |
| `/update` | Le résumé du matin, tout de suite |
| `/update all` | État actuel de **toutes** les entités, **regroupées par appareil** (les capteurs d'une prise sous son interrupteur), avec l'heure du dernier changement |
| `/on <nom>` · `/off <nom>` | Allume / éteint l'interrupteur d'un appareil (voir *Piloter un interrupteur*) |
| `/stale [heures]` | Entités **sans nouvelle depuis N h** (24 par défaut), avec leur âge : le moyen de repérer les appareils d'une ancienne installation |

`/list` et `/update all` n'affichent ni les entités **désactivées** dans Home Assistant (entité ou intégration : elles
n'existent pas dans son API), ni les entités « fantômes » (`restored`, intégration non chargée), ni, par défaut,
les entités actuellement **indisponibles**. Un pied de page indique combien sont masquées. Pour tout voir :
`"hide_unavailable": false` dans `config.json` (les fantômes restent masqués). Le résumé du matin n'est pas filtré
sur l'état actuel : un capteur tombé en panne en cours de journée y figure encore avec ses mesures.
| `/stop ping` / `/start ping` | Coupe / relance le heartbeat Healthchecks (⚠️ couper = alerte après la grâce) |
| `/stop monitor` / `/start monitor` | Coupe / relance le résumé du matin (le cron tourne, mais n'envoie rien) |
| `/shutdown` (alias `/stop pi`) | Éteint le Pi après confirmation par `/shutdown confirm` (voir *Éteindre le Pi à distance*) |
| `/arm all` · `/arm <groupe>` · `/arm <appareil>` | Arme les groupes définis dans `config.json`, ou un appareil d'un groupe (voir *Armement*) |
| `/disarm all` · `/disarm <groupe>` · `/disarm <appareil>` | Les désarme (alias : `/unarm`) |
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

### Résumé du matin : détecteurs, aspirateur, lignes masquées
- **🛡 Détecteurs / caméras** : les entités de la config `arm` (groupes et surnoms), les caméras et les détecteurs de mouvement
  (nom contenant motion, mouvement, pir, camera, détecteur, présence) y sont regroupés avec leur appareil. Une entité armable
  affiche son état (`armé` / `désarmé`) et ses changements sur la fenêtre.
- **Aspirateur** (appareil ayant une entité `vacuum.*`, ex. Roomba) : seule la ligne « Total missions » est toujours affichée,
  avec `(-)` si elle n'a pas varié ; les autres lignes n'apparaissent que si leur valeur a changé sur 24 h.
- **Débits (KiB/s, Mbit/s…)** : au lieu de la vitesse, le résumé donne le **volume** échangé sur la fenêtre (intégrale des relevés
  du débit, donc approximatif) et le **pic** avec son heure : `Download speed : ≈ 1,20 Go sur 24 h · pic 222KiB/s à 14h52`.
- **Rattacher une entité à un bloc** : `"summary": {"attach": {"input_boolean.motion_sensor_armed": "Mezzanine motion detector"}}`
  place une entité sans appareil (un helper) sous le bloc de l'appareil de ce nom (le nom doit être identique).
- **Lignes masquées** : `"summary": {"hide": ["texte"]}` (vide par défaut) retire du résumé du matin tout capteur dont le nom
  contient ce texte. `/update all` montre toujours tout.
- Batteries : une batterie dont le nom contient « batter » est masquée au-dessus du seuil, même sans unité.

### Résumé du matin : appareils sans interrupteur et batteries
Les capteurs/alertes d'un appareil sans interrupteur (ex. détecteur PIR) sont regroupés sous « 🏠 Appareils » (`■ nom` puis ses lignes).
Les batteries (%) au-dessus de 30 % sont masquées ; à 30 % ou moins elles apparaissent avec ⚠️.
Seuil réglable : `"summary": {"battery_warn": 30}`.

### Résumé du matin : les appareils à interrupteur
Pour chaque appareil qui a un interrupteur (prise connectée…), le résumé tient sur **une ligne** : nombre de cycles et heures,
statut actuel, énergie consommée sur la fenêtre, par exemple
`Déshumidificateur entrée : 2 cycles (07h10, 19h40), allumé · énergie totale 5,04 kWh (+1,20 kWh sur 24 h)`. Toutes les prises connectées sont listées, même sans cycle (« aucun cycle, éteint »).
- La puissance, le courant et la tension ne sont **pas** affichés. La tension n'est signalée que si elle sort de la plage
  normale (`⚠️ tension 186–231 V`), réglable : `"summary": {"voltage_range": [200, 250]}`. Les 0 V (prise hors mesure) sont ignorés.
- L'énergie est la **consommation sur la fenêtre**, calculée d'après le compteur cumulatif (kWh, Wh ou MWh) de l'appareil ;
  une baisse du compteur est comptée comme une remise à zéro.
- Un appareil sans activité (jamais allumé, éteint, 0 énergie, tension normale) n'apparaît pas.
- Ses autres interrupteurs (verrou enfant, rétro-éclairage) et tous ses capteurs sont absorbés dans cette ligne ; les
  capteurs de mouvement (`binary_sensor`) restent dans « Alertes ».
- L'interrupteur principal est celui qui porte le nom de l'appareil, sinon celui dont l'`entity_id` est le plus court.
`/update all` garde **tout**, regroupé par appareil. Si le regroupement échoue, le résumé retombe sur une ligne par entité.

### Piloter un interrupteur (`/on`, `/off`)
`/on déshumidificateur entrée` allume l'interrupteur de l'appareil, `/off …` l'éteint. Le nom est cherché dans le nom de
l'appareil, de l'entité et son `entity_id`, sans casse ni accents, mots dans n'importe quel ordre. S'il désigne plusieurs
appareils (« déshumidificateur » → entrée et garage), rien n'est actionné et le bot liste les candidats, sauf si l'un porte
exactement ce nom. `/on` seul liste les appareils pilotables.
- Seul l'**interrupteur principal** est actionné ; les autres interrupteurs de l'appareil sont signalés « non touchés ».
- La réponse relit l'état 2 s après l'ordre ; une erreur interne de Home Assistant est retentée une fois.
```json
"control": {
  "domains": ["switch"],
  "exclude": ["switch.prise_routeur", "switch.congelateur"]
}
```
`domains` : types pilotables (`switch` par défaut ; permis : `switch`, `light`, `input_boolean`, `fan`). `exclude` : `entity_id`
exacts ou préfixes à **ne jamais** piloter. Mettez-y tout ce dont l'arrêt à distance serait gênant : la prise du routeur ou
du Pi, du congélateur… Comme pour toute commande, seul votre `telegram_chat_id` est écouté.

### Armement (`/arm`, `/disarm`)
(Dans ce README, les menus de Home Assistant sont donnés avec leurs noms anglais.)
`/arm all` met en marche les caméras et détecteurs que **vous** listez dans `config.json`, par groupes ; `/disarm` (alias
`/unarm`) fait l'inverse. `/arm` seul affiche les groupes et les entités que chacun toucherait, sans rien actionner.

La cible peut être `all`, un **groupe**, un **surnom**, ou le **nom d'un appareil** (ou d'une entité) : `/arm caméra jardin`,
`/disarm arlo terrasse`. Les accents et la casse sont ignorés. Un nom d'appareil ne peut viser que des entités **déjà
présentes dans vos groupes** : une prise de chauffage dont le nom contient « salon » n'est jamais touchée par
`/arm salon`. Si le nom correspond à plusieurs appareils (« salon » → caméra et détecteur), rien n'est actionné et le bot
liste les candidats, sauf si l'un d'eux porte exactement ce nom.
```json
"arm": {
  "groups": {
    "netatmo":  {"prefixes": ["camera.netatmo_"]},
    "cameras":  {"entities": ["camera.arlo_terrasse", "camera.arlo_garage"]},
    "presence": {"entities": ["switch.detecteur_salon", "input_boolean.alarme_armee"]}
  }
}
```
Un groupe peut lister des `entities` exactes et/ou des `prefixes` d'`entity_id`. Pour trouver les noms : `/list netatmo`,
`/list camera`, `/list presence` (le détail des entités d'un appareil). Après modification : valider avec
`python3 -c "import common; print('config OK')"`, puis `sudo systemctl restart housemonitor-listener`.

`/arm` appelle `<domaine>.turn_on` sur chaque entité, `/disarm` `<domaine>.turn_off`. Un appel en erreur interne (HTTP 5xx, par
exemple un échec passager du cloud d'un fabricant) est retenté une fois après 3 s ; la réponse le signale (« ↻ réussi au 2e essai »). La réponse relit l'état de chaque
entité 2 s après l'ordre et signale les entités introuvables, indisponibles ou dont le service échoue.

**Surnoms** : un nom court de votre choix pour une ou plusieurs entités, indépendant des noms d'appareils de Home Assistant
(qui peuvent changer). Il est cherché avant la recherche par nom, sans tenir compte de la casse ni des accents ; un groupe de
même nom l'emporterait. `/arm all` n'agit que sur les **groupes** : une entité qui n'a qu'un surnom n'est pas armée par `all`.
```json
"arm": {
  "groups": {"cameras":  {"entities": ["camera.living_room_camera"]},
             "presence": {"entities": ["input_boolean.motion_sensor_armed"]}},
  "nicknames": {"living room camera": "camera.living_room_camera",
                "motion sensor": "input_boolean.motion_sensor_armed"}
}
```
Un surnom est soumis aux mêmes garde-fous : une sirène ou une lumière nommée par erreur est refusée.

Garde-fous : seuls les domaines `camera`, `switch`, `input_boolean` et `automation` sont autorisés. Une sirène, une
lumière, une serrure ou un volet listés par erreur sont refusés et jamais actionnés. Comme pour toutes les commandes, seul
votre `telegram_chat_id` est écouté.

À savoir :
- Ce que fait `turn_on` dépend de l'intégration : pour une caméra Netatmo, c'est à ma connaissance l'activation de la
  surveillance. Vérifiez sur un groupe d'essai que l'état relu change bien.
- Un **détecteur de mouvement** n'est généralement qu'un capteur en lecture seule : Home Assistant ne peut pas le « mettre
  en marche ». Soit son intégration expose un interrupteur ou une automatisation, qu'on met alors dans le groupe, soit on
  passe par un **helper** de type Toggle (`input_boolean.motion_sensor_armed`, créé dans Settings → Devices & services → Helpers → Create helper → Toggle) que vos automatisations d'alerte testent comme condition, et
  qu'on met dans le groupe.
- Les alarmes (`alarm_control_panel`), qui demandent souvent un code, ne sont pas gérées.

### Première alerte du jour (Telegram)
Le listener surveille les détecteurs et caméras listés dans `alerts.watch` et envoie **un seul** message par appareil et par
jour : `🚨 Première alerte du jour — PIR à 14h43`. La lecture se fait en local sur HA toutes les 20 s (aucune data 4G),
seul le message utilise la 4G.

```json
"alerts": {
  "armed_by": "input_boolean.motion_sensor_armed",
  "watch": [
    {"entity": "binary_sensor.pir_motion", "name": "PIR"},
    {"entity": "camera.living_room_camera", "name": "Caméra salon", "state": "recording"}
  ]
}
```
- `armed_by` : si cette entité est « off » (`/disarm`), aucune alerte n'est envoyée et la 1re alerte du jour n'est pas consommée.
  Il s'applique à tous les appareils de `watch`, sauf si une entrée a son propre `armed_by`. Pour une caméra, mettre
  l'entité de la caméra elle-même : `{"entity": "...", "name": "Caméra salon", "armed_by": "camera.living_room_camera"}`.
- États déclencheurs par défaut : `on` (binary_sensor), `recording` (camera) ; pour une entité `event.*`, tout nouvel événement.
  Pour trouver l'état d'une caméra qui détecte un mouvement : HA → Developer tools → States, filtrez sur la caméra.
- **SMS en plus** : `"sms_service": "notify.sms_moi"` dans le bloc `alerts` envoie aussi la première alerte par SMS, via un
  service `notify` de HA (par ex. celui du routeur 4G TP-Link, voir ci-dessous). Sans cette clé, pas de SMS ; un échec du SMS
  n'empêche jamais le message Telegram. Test depuis HA : Developer tools → Actions → `notify.sms_moi`.
- **SMS par le routeur 4G TP-Link TL-MR6400** (firmware 1.0.x, sans HA) : bloc `"sms": {"host", "user", "password", "to",
  "max_per_day"}` dans `config.json` (le mot de passe du routeur reste dans ce fichier, jamais dans git). Test : `python3 routersms.py "texte"`.
  Le protocole vient de l'interface web du routeur (pas de documentation officielle). Se connecter déconnecte l'interface web
  du routeur (un seul administrateur à la fois) ; après un échec de connexion plus aucune tentative pendant 1 h (le routeur
  bloque 2 h après 10 échecs) ; 5 SMS par jour au maximum. Les SMS sont facturés selon votre forfait.
- **`/rearm`** remet la journée à zéro : la prochaine alerte de chaque appareil est envoyée (Telegram et SMS), puis de nouveau
  une seule fois jusqu'au lendemain. Les mouvements antérieurs à `/rearm` ne sont pas rejoués.
- Un fichier `alerts_state.json` (ignoré par git) retient les appareils déjà alertés aujourd'hui. Redémarrer le listener
  après modification de `config.json` : `sudo systemctl restart housemonitor-listener`.

### Armer / désarmer pour une durée
`/arm all 30` (minutes), `/disarm all 24h`, `/disarm motion sensor 2d` (unités m, h, d ; 1 minute à 30 jours). À l'échéance, les
entités reviennent à l'état qu'elles avaient **avant** la commande et un message Telegram le confirme. Une nouvelle commande
sur les mêmes entités annule la minuterie précédente (l'état d'origine est conservé). `/arm` seul liste les minuteries en cours.
Un nombre sans unité est des minutes, sauf si le texte entier est un nom d'appareil (« camera 2 »). Les minuteries sont dans
`arm_timers.json` (ignoré par git) et survivent au redémarrage du listener ; le Pi n'a pas d'horloge à pile, donc juste
après un reboot l'heure peut être fausse jusqu'à la synchro réseau.

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

Healthchecks n'a pas de signal « arrêt » : ses points d'entrée sont `/start`, `/fail`, `/log` et les codes de sortie.
`/log` est le plus proche d'un arrêt voulu qui n'est pas un échec.

**Tester sans éteindre** : `"shutdown": {"dry_run": true}` dans `config.json` (puis
`sudo systemctl restart housemonitor-listener`). `/shutdown` exécute alors toute la chaîne (confirmation, acquittement,
signal Healthchecks, enregistrement de la data), puis remet le check au vert et répond « 🧪 Simulation : le Pi n'a PAS été
éteint ». Remettez `false` pour le vrai arrêt.

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
Un ping qui échoue (réseau coupé un instant) est **retenté 5 minutes plus tard**, et non au prochain créneau de 4 h : avec une
période de 4 h et 1 h de grâce, un seul ping manqué suivi d'une attente de 4 h déclencherait une fausse alerte.
Le listener pinge toutes les `healthchecks.ping_interval_hours` (4 h, ~6 Ko/ping ≈ 1 Mo/mois),
et le résumé du matin pinge aussi. Avec période 4 h + grâce 1 h, une panne est signalée en 5 h max.

Comportement :
- run OK → ping → Healthchecks attend le prochain run.
- run en erreur (HA injoignable, Telegram KO…) → ping `/fail` → **alerte immédiate**.
- Pi éteint / plus de 4G / listener mort → aucun ping → **alerte après période + grâce** (≤ 4h30).

## Dépannage : « entité ignorée » dans le résumé
Si une entité a des données illisibles dans l'historique (par exemple une marque de suppression après avoir retiré puis
rajouté une intégration), le résumé l'ignore, part quand même, et ajoute en pied de message `⚠️ N entités ignorées : …`.
Le détail (type d'erreur) est dans `run.log`. Un résumé qui échoue complètement envoie à Healthchecks la trace de
l'erreur, visible dans l'onglet *Log* du check, sur la ligne du ping en échec.

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

## Dépannage : des appareils d'une ancienne installation apparaissent encore
Après un déménagement, le résumé du matin peut montrer l'ancienne installation jusqu'à 24 h (il lit l'historique des
dernières 24 h) ; `/list` et `/update all` montrent l'état actuel, sans fenêtre de temps. Si des appareils restent
visibles dans `/list`, c'est qu'ils existent encore dans Home Assistant. Envoyez `/stale 24` : vous obtenez la liste
des entités muettes, avec leur âge, puis désactivez l'intégration correspondante (Settings → Devices & services).
Une entité désactivée dans Home Assistant disparaît de son API, donc du résumé et de `/list`.
Une valeur figée (MQTT, capteur « template », bouton laissé sur `on`) n'est jamais « indisponible » : seul `/stale` la montre.

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
