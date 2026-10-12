# HouseMonitor

Envoie chaque matin sur Telegram un résumé (min / max / actuel sur 24 h) de capteurs Home Assistant. Python 3 seulement, aucune dépendance.

## Installation (Raspberry Pi)
1. Home Assistant : Profil → Sécurité → *Jetons d'accès longue durée* → créer un jeton.
2. Telegram : créer un bot avec @BotFather (token), envoyer un message au bot, puis ouvrir
   `https://api.telegram.org/bot<TOKEN>/getUpdates` pour lire `chat.id`.
3. `cp config.example.json config.json` et remplir (entités : Outils de développement → États).
4. Tester : `python3 summary.py --dry` (affiche sans envoyer), puis `python3 summary.py`.
5. Cron (`crontab -e`) pour 7h30 : `30 7 * * * /usr/bin/python3 /home/pi/HouseMonitor/summary.py`
