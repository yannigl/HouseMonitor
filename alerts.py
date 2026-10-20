"""Première alerte du jour de chaque détecteur / caméra, envoyée sur Telegram (tourne dans un thread du listener).

config.json -> "alerts": {
    "armed_by": "input_boolean.motion_sensor_armed",      # facultatif : pas d'alerte si ce helper est « off » (/disarm)
    "sms_service": "notify.sms_moi",                        # facultatif : SMS en plus de Telegram (service notify de HA)
    "poll_seconds": 20,                                    # lecture de l'historique HA (local : aucune data 4G)
    "watch": [
      {"entity": "binary_sensor.pir_motion", "name": "PIR"},
      {"entity": "camera.living_room_camera", "name": "Caméra salon", "state": "recording"}
    ]}
Une entrée peut être une simple chaîne (entity_id). États déclencheurs par défaut : « on » (binary_sensor…),
« recording » (camera) ; pour une entité event.*, tout nouvel événement. `state` et `armed_by` peuvent être précisés par entrée.
Seule la première alerte de la journée est envoyée pour chaque appareil, et seulement s'il est armé (la 1re alerte d'un
appareil désarmé n'est pas consommée). Le détail du jour est dans alerts_state.json.
"""
import json
import sys
import threading
import time
import urllib.parse
from datetime import datetime, timedelta, timezone

import routersms
import summary
from common import CFG, HERE

CONF = CFG.get("alerts", {})
FILE = HERE / "alerts_state.json"
DEFAULT_STATE = {"camera": "recording"}


def watches():
    out = []
    for w in CONF.get("watch", []):
        w = {"entity": w} if isinstance(w, str) else dict(w)
        dom = w["entity"].split(".")[0]
        w.setdefault("name", w["entity"])
        w.setdefault("state", None if dom == "event" else DEFAULT_STATE.get(dom, "on"))
        w.setdefault("armed_by", CONF.get("armed_by"))
        out.append(w)
    return out


LOCK = threading.Lock()  # le thread de surveillance et /rearm écrivent le même fichier


def rearm():
    """/rearm : remet la journée à zéro, la prochaine alerte de chaque appareil sera envoyée (Telegram et SMS).
    Retourne le texte de réponse."""
    with LOCK:
        done = load_done()
        n = len(done["done"])
        FILE.write_text(json.dumps({"day": done["day"], "done": []}))
    names = ", ".join(w["name"] for w in watches()) or "aucun appareil configuré (alerts.watch)"
    return (f"🔔 Alertes réarmées : la prochaine alerte de chaque appareil sera envoyée.\n"
            f"Surveillés : {names}\nDéjà alertés aujourd'hui avant /rearm : {n}")


def load_done():
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        d = json.loads(FILE.read_text())
        if d.get("day") == today:
            return d
    except (OSError, ValueError):
        pass
    return {"day": today, "done": []}


def triggers(w, series, since):
    """Premier événement déclencheur postérieur à `since` dans l'historique d'une entité, sinon None."""
    for s in series:
        t = datetime.fromisoformat(s.get("last_changed") or s["last_updated"])
        if t <= since or s["state"] in ("unavailable", "unknown"):
            continue
        if w["state"] is None or s["state"] == w["state"]:
            return t.astimezone()
    return None


def sms(text):
    """SMS en plus de Telegram. Deux voies, la première configurée l'emporte :
    - "sms" dans config.json : envoi direct par le routeur 4G TP-Link (routersms.py) ;
    - "alerts.sms_service" : un service notify de HA (ex. « notify.sms_moi »).
    Une erreur d'envoi du SMS ne doit jamais empêcher l'alerte Telegram."""
    try:
        if CFG.get("sms"):
            ok, detail = routersms.send(text)
            if not ok:
                print(f"SMS KO : {detail}", file=sys.stderr, flush=True)
            return
        service = CONF.get("sms_service")
        if not service:
            return
        dom, name = service.split(".", 1)
        summary.http(f"{summary.HA}/api/services/{dom}/{name}", {**summary.AUTH, "Content-Type": "application/json"},
                     json.dumps({"message": text}).encode())
    except Exception as e:
        print(f"SMS KO : {type(e).__name__}: {e}", file=sys.stderr, flush=True)


def check(since):
    """Une passe : envoie les alertes dues. Retourne le nombre d'alertes envoyées."""
    ws = watches()
    if not ws:
        return 0
    done = load_done()
    todo = [w for w in ws if w["entity"] not in done["done"]]
    if not todo:
        return 0
    qs = urllib.parse.urlencode({"filter_entity_id": ",".join(w["entity"] for w in todo),
                                 "minimal_response": "", "no_attributes": ""})
    data = summary.http(f"{summary.HA}/api/history/period/{since.isoformat()}?{qs}", summary.AUTH)
    series = {lst[0]["entity_id"]: lst for lst in data if lst}
    sent = 0
    for w in todo:
        t = triggers(w, series.get(w["entity"], []), since)
        if not t:
            continue
        if w["armed_by"]:
            armed = summary.http(f"{summary.HA}/api/states/{w['armed_by']}", summary.AUTH).get("state")
            if armed != "on":
                continue  # désarmé : l'alerte n'est pas consommée
        msg = f"🚨 Première alerte du jour — {w['name']} à {t:%Hh%M}"
        summary.send(msg)
        sms(msg)
        with LOCK:
            done = load_done()
            done["done"].append(w["entity"])
            FILE.write_text(json.dumps(done))
        sent += 1
    return sent


def run():
    poll = max(5, CONF.get("poll_seconds", 20))
    since = datetime.now(timezone.utc) - timedelta(minutes=2)  # recouvre le redémarrage du listener
    print(f"alertes : {len(watches())} appareil(s) surveillé(s)", flush=True)
    while True:
        now = datetime.now(timezone.utc)
        try:
            check(since)
            since = now - timedelta(seconds=2)  # petit recouvrement ; chaque appareil n'alerte qu'une fois par jour
        except Exception as e:  # HA ou Telegram momentanément injoignable : on réessaie à la passe suivante
            print(f"alertes KO : {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        time.sleep(poll)


def start():
    if CONF.get("watch"):
        threading.Thread(target=run, daemon=True, name="alerts").start()
