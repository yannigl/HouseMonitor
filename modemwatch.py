"""Surveillance d'Internet et redémarrage du modem 4G (routeur TL-MR6400), dans un thread du listener.

config.json -> "modem": {"auto_restart": true, "down_minutes": 10, "max_per_day": 2, "min_gap_minutes": 30,
                         "check": [["1.1.1.1", 443], ["8.8.8.8", 53]]}
(identifiants du routeur : le bloc « sms », le même routeur)
  - Internet est « coupé » quand aucune des adresses de `check` ne répond ;
  - coupé depuis `down_minutes` minutes ET routeur joignable sur le réseau local : redémarrage du modem, au plus `max_per_day` par 24 h et
    espacés d'au moins `min_gap_minutes` (une vraie panne d'opérateur ne le fait pas redémarrer en boucle) ;
  - au retour d'Internet, un message Telegram dit la durée de la coupure et ce qui a été fait (Telegram n'est pas joignable pendant la coupure).
  - `/restart modem` (administrateurs, avec confirmation) redémarre le modem à la demande ; ne fait JAMAIS de remise aux réglages d'usine.
"""
import json
import socket
import sys
import threading
import time
from datetime import datetime

import routersms
import summary
from common import CFG, HERE

CONF = CFG.get("modem", {})
AUTO = CONF.get("auto_restart", True)
DOWN_AFTER = CONF.get("down_minutes", 10) * 60
MAX_PER_DAY = CONF.get("max_per_day", 2)
MIN_GAP = CONF.get("min_gap_minutes", 30) * 60
CHECK = [tuple(x) for x in CONF.get("check", [["1.1.1.1", 443], ["8.8.8.8", 53]])]
EVERY = 30  # s entre deux tests de connexion
MIN_NOTIFY = 120  # s : une coupure plus courte ne mérite pas de message
STATE = HERE / "modem_state.json"


def internet_up():
    for host, port in CHECK:
        try:
            socket.create_connection((host, port), timeout=4).close()
            return True
        except OSError:
            continue
    return False


def router_reachable():
    host = CFG.get("sms", {}).get("host", "192.168.1.1")
    try:
        socket.create_connection((host.split(":")[0], int(host.split(":")[1]) if ":" in host else 80), timeout=4).close()
        return True
    except OSError:
        return False


def _times():
    try:
        times = json.loads(STATE.read_text()).get("times", [])
    except (OSError, ValueError):
        times = []
    now = time.time()
    return [t for t in times if t <= now]  # l'horloge du Pi (sans pile) a pu reculer : une date « dans le futur » est ignorée


def allowed(now=None):
    """Un redémarrage automatique est-il permis maintenant ? (plafond sur 24 h et écart minimal) -> (bool, raison)."""
    now = now or time.time()
    recent = [t for t in _times() if now - t < 86400]
    if len(recent) >= MAX_PER_DAY:
        return False, f"plafond de {MAX_PER_DAY} redémarrages par 24 h atteint"
    if recent and now - max(recent) < MIN_GAP:
        return False, f"dernier redémarrage il y a moins de {MIN_GAP // 60} min"
    return True, ""


def record():
    STATE.write_text(json.dumps({"times": (_times() + [time.time()])[-20:]}))


def fmt(sec):
    sec = int(sec)
    return f"{sec // 60} min" if sec >= 120 else f"{sec} s"


def wait_for_internet(timeout=900):
    """Attend le retour d'Internet après un redémarrage demandé. -> secondes écoulées, ou None si pas revenu."""
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        time.sleep(10)
        if internet_up():
            return time.monotonic() - t0
    return None


def run():
    time.sleep(60)  # laisser le Pi et le réseau démarrer
    print(f"modem : surveillance d'Internet (redémarrage auto {'actif' if AUTO else 'désactivé'}, coupure > {DOWN_AFTER // 60} min)", flush=True)
    down_since, rebooted, note = None, None, ""
    while True:
        try:
            if internet_up():
                if down_since is not None:
                    dur = time.monotonic() - down_since
                    if dur >= MIN_NOTIFY:
                        said = f"modem redémarré automatiquement à {rebooted:%Hh%M}" if rebooted else "sans intervention"
                        summary.send(f"📶 Internet de retour après {fmt(dur)} de coupure ({said}){(chr(10) + note) if note else ''}")
                down_since, rebooted, note = None, None, ""
            else:
                now = time.monotonic()
                if down_since is None:
                    down_since = now
                    print("modem : Internet coupé", flush=True)
                elif AUTO and rebooted is None and now - down_since >= DOWN_AFTER:
                    ok_to, why = allowed()
                    if not ok_to:
                        note = f"⚠️ pas de redémarrage automatique : {why}"
                        rebooted = False  # ne pas réessayer pendant cette coupure
                    elif not router_reachable():
                        note = "⚠️ le routeur ne répondait pas sur le réseau local : redémarrage impossible"
                        rebooted = False
                    else:
                        ok, detail = routersms.reboot()
                        print(f"modem : redémarrage automatique → {ok} ({detail})", flush=True)
                        if ok:
                            record()
                            rebooted = datetime.now()
                        else:
                            note = f"⚠️ redémarrage automatique du modem refusé : {detail}"
                            rebooted = False
        except Exception as e:  # ne jamais arrêter la surveillance
            print(f"modem : erreur {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        time.sleep(EVERY)


def restart_now(chat_id=None):
    """/restart modem confirmé : redémarre le modem, puis prévient quand Internet revient. -> (ok, détail)."""
    ok, detail = routersms.reboot()
    if ok:
        record()  # compte dans le plafond des redémarrages automatiques

        def back():
            dur = wait_for_internet()
            summary.send("📶 Modem de retour : Internet disponible après " + fmt(dur) + "." if dur is not None
                         else "⚠️ Le modem a été redémarré mais Internet n'est pas revenu après 15 min.", also=[chat_id])
        threading.Thread(target=back, daemon=True, name="modem-back").start()
    return ok, detail


def start():
    if CFG.get("sms", {}).get("host") and CFG.get("sms", {}).get("password"):
        threading.Thread(target=run, daemon=True, name="modem-watch").start()
