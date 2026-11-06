"""Compteur de trafic du routeur 4G TP-Link lui-même (module « status » de son interface web : wan.dailyStatistics, wan.totalStatistics).

Contrairement aux capteurs de débit de HA (relevés toutes les 30 s, sans les appels vidéo), c'est le compteur du routeur : tout ce qui passe par la 4G,
Wi-Fi compris. Un thread du listener le lit toutes les `router_counter.every_minutes` minutes (30 par défaut, 0 = jamais) et garde les relevés dans
router_counter.csv (reste sur le Pi) : `totalStatistics` est un cumul, la différence entre deux relevés donne le volume de la période ;
`dailyStatistics` est le volume du jour (remis à zéro à minuit). Chaque lecture ouvre une session d'administration : l'interface web du routeur est
déconnectée quelques secondes.

config.json -> "router_counter": {"every_minutes": 30}        (nécessite le bloc "sms" : adresse, utilisateur et mot de passe du routeur)
"""
import json
import sys
import threading
import time
from datetime import datetime, timedelta

import routersms
from common import CFG, HERE

CONF = CFG.get("router_counter", {})
EVERY = CONF.get("every_minutes", 30) * 60
LOG = HERE / "router_counter.csv"  # heure ISO locale, total, jour, débit rx, débit tx (octets, octets/s)
KEEP_DAYS = 90
LOCK = threading.Lock()


def read():
    """Une lecture du routeur -> {"total", "daily", "rx", "tx"} en octets (et octets/s), ou None si le routeur est occupé / injoignable."""
    try:
        with routersms.router_session(wait=20) as router:
            raw = router._req(router.base + "lteWebCfg", json.dumps({"module": "status", "action": 0}))
        w = json.loads(raw)["wan"]
        return {"total": float(w["totalStatistics"]), "daily": float(w["dailyStatistics"]), "rx": float(w.get("rxSpeed") or 0), "tx": float(w.get("txSpeed") or 0)}
    except routersms.RouterBusy as e:
        print(f"compteur du routeur : {e}", file=sys.stderr, flush=True)
    except Exception as e:
        print(f"compteur du routeur illisible : {type(e).__name__}: {e}", file=sys.stderr, flush=True)
    return None


def load():
    """[(datetime locale, total, jour, rx, tx)] du plus ancien au plus récent."""
    rows = []
    try:
        for line in LOG.read_text().splitlines():
            p = line.split(",")
            try:
                rows.append((datetime.fromisoformat(p[0]), float(p[1]), float(p[2]), float(p[3]), float(p[4])))
            except (ValueError, IndexError):
                pass
    except OSError:
        pass
    return rows


def record():
    """Lit le routeur et ajoute le relevé au journal. -> le relevé ou None."""
    r = read()
    if not r:
        return None
    now = datetime.now()
    with LOCK:
        rows = load()
        keep = [x for x in rows if x[0] > now - timedelta(days=KEEP_DAYS)]
        if len(keep) != len(rows):
            LOG.write_text("".join(f"{t.isoformat(timespec='seconds')},{a:.0f},{b:.0f},{c:.0f},{d:.0f}\n" for t, a, b, c, d in keep))
        with open(LOG, "a") as f:
            f.write(f"{now.isoformat(timespec='seconds')},{r['total']:.0f},{r['daily']:.0f},{r['rx']:.0f},{r['tx']:.0f}\n")
    return r


def delta(rows, a, b):
    """Octets entre les relevés a et b (index) ; un cumul qui redescend (remise à zéro du routeur) compte la valeur lue en entier."""
    total = 0.0
    for i in range(a + 1, b + 1):
        d = rows[i][1] - rows[i - 1][1]
        total += d if d >= 0 else rows[i][1]
    return total


def volume(hours=24, rows=None):
    """Octets des `hours` dernières heures d'après le journal, ou None s'il n'y a pas assez de relevés (relevé de départ à plus de 45 min de l'heure voulue)."""
    rows = rows if rows is not None else load()
    if len(rows) < 2:
        return None
    target = rows[-1][0] - timedelta(hours=hours)
    i = min(range(len(rows)), key=lambda k: abs((rows[k][0] - target).total_seconds()))
    if abs((rows[i][0] - target).total_seconds()) > 45 * 60 or i >= len(rows) - 1:
        return None
    return delta(rows, i, len(rows) - 1)


def day_volume(day, rows=None):
    """Volume (octets) du jour `day` (date) : dernier compteur du jour + ce qui a été compté entre ce relevé et minuit. None s'il n'y a pas de relevé ce jour-là."""
    rows = rows if rows is not None else load()
    idx = [i for i, r in enumerate(rows) if r[0].date() == day]
    if not idx:
        return None
    last = idx[-1]
    v = rows[last][2]
    nxt = last + 1
    if nxt < len(rows) and rows[nxt][0].date() == day + timedelta(days=1):  # trafic entre le dernier relevé du jour et minuit
        v += max(0.0, delta(rows, last, nxt) - rows[nxt][2])
    return v


def text():
    """Lignes pour /data et le résumé : compteur du routeur, aujourd'hui / hier / 24 h. '' si pas encore de relevé."""
    rows = load()
    if not rows or datetime.now() - rows[-1][0] > timedelta(hours=3):
        return ""
    from common import fmt_bytes
    today = datetime.now().date()
    parts = [f"aujourd'hui {fmt_bytes(rows[-1][2])} (en cours)"]
    y = day_volume(today - timedelta(days=1), rows)
    if y is not None:
        parts.append(f"hier {fmt_bytes(y)}")
    v = volume(24, rows)
    if v is not None:
        parts.append(f"24 h {fmt_bytes(v)}")
    return "📶 Compteur du routeur 4G : " + " · ".join(parts)


def run():
    time.sleep(90)  # le Pi et le réseau démarrent
    while True:
        record()
        time.sleep(EVERY)


def start():
    if EVERY > 0 and CFG.get("sms", {}).get("host") and CFG.get("sms", {}).get("password"):
        threading.Thread(target=run, daemon=True, name="router-counter").start()


if __name__ == "__main__":  # python3 routercount.py : une lecture, enregistrée, et le texte affiché
    r = record()
    print(r if r else "❌ lecture impossible (voir le message ci-dessus)")
    print(text())
