"""Partagé entre summary.py et listener.py : config, état, compteur data, heartbeat."""
import calendar
import json
import sys
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).parent
CFG = json.loads((HERE / "config.json").read_text())
STATE_FILE = HERE / "state.json"
CYCLE_FILE = HERE / "data_cycle.json"
NETLOG = HERE / "netlog.csv"
HC_URL = CFG.get("healthchecks", {}).get("url", "")
TLS_OVERHEAD = 6000  # ~octets d'un handshake TLS (estimation, invisible depuis Python)
NET = {"sent": 0, "recv": 0}  # octets internet du processus courant (HA est en local)

SERVICES = ("listener", "ping", "monitor")


def load_state():
    """{"listener": bool, "ping": bool, "monitor": bool} ; tout actif par défaut."""
    try:
        st = json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        st = {}
    return {s: st.get(s, True) for s in SERVICES}


def save_state(st):
    STATE_FILE.write_text(json.dumps(st))


def count(sent=0, recv=0, new_connection=False):
    NET["sent"] += sent + (TLS_OVERHEAD if new_connection else 0)
    NET["recv"] += recv


def heartbeat(ok=True, body=""):
    """Ping Healthchecks.io ; /fail déclenche une alerte immédiate, aucun ping -> alerte après la grâce.
    `body` (erreur, trace) est visible dans l'onglet Log du check, sur la ligne de ce ping."""
    if not HC_URL or not load_state()["ping"]:
        return False
    url = HC_URL if ok else HC_URL.rstrip("/") + "/fail"
    try:
        req = urllib.request.Request(url, data=body[-2000:].encode(), headers={"User-Agent": "housemonitor"})
        with urllib.request.urlopen(req, timeout=15) as r:
            count(sent=300, recv=len(r.read()) + 200, new_connection=True)
        return True
    except Exception as e:  # ne doit jamais faire échouer l'appelant
        print(f"heartbeat KO : {e}", file=sys.stderr)
        return False


def log_net(source, git_bytes=0):
    """Ajoute une ligne à netlog.csv : date, source, octets git pull, octets telegram/ping, total."""
    tg = NET["sent"] + NET["recv"]
    new = not NETLOG.exists()
    with NETLOG.open("a") as f:
        if new:
            f.write("date,source,git_pull_octets,telegram_octets,total_octets\n")
        f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S},{source},{git_bytes},{tg},{git_bytes + tg}\n")
    NET["sent"] = NET["recv"] = 0


# --- cycle de comptage de la data : du jour de reset au même jour du mois suivant ---

def _next_boundary(start, day):
    """Minuit du jour `day` du mois suivant `start` (ramené à la fin du mois si besoin : 31 -> 30/28)."""
    y, m = (start.year + 1, 1) if start.month == 12 else (start.year, start.month + 1)
    return datetime(y, m, min(day, calendar.monthrange(y, m)[1]))


def cycle():
    """(début du cycle, jour d'ancrage). Par défaut : le 1er du mois courant. Avance tout seul."""
    now = datetime.now()
    try:
        c = json.loads(CYCLE_FILE.read_text())
        start, day = datetime.fromisoformat(c["start"]), int(c["day"])
    except (OSError, ValueError, KeyError):
        start, day = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0), 1
    rolled = False
    while now >= _next_boundary(start, day):
        start, rolled = _next_boundary(start, day), True
    if rolled or not CYCLE_FILE.exists():
        CYCLE_FILE.write_text(json.dumps({"start": start.isoformat(), "day": day}))
    return start, day


def reset_cycle():
    """/data reset : le compteur repart de 0 maintenant, prochain reset le même jour du mois suivant."""
    now = datetime.now()
    CYCLE_FILE.write_text(json.dumps({"start": now.isoformat(timespec="seconds"), "day": now.day}))


def cycle_total():
    """Octets internet du cycle courant : netlog.csv depuis le début du cycle + compteur du process en cours."""
    start, _ = cycle()
    since = f"{start:%Y-%m-%d %H:%M:%S}"
    total = NET["sent"] + NET["recv"]
    if NETLOG.exists():
        total += sum(int(l.split(",")[-1]) for l in NETLOG.read_text().splitlines()[1:]
                     if l.split(",")[0] > since)
    return total


def cycle_line():
    """Ex. : '312 Ko depuis le 13/10 (remise à zéro auto le 13/11)'."""
    start, day = cycle()
    return (f"{cycle_total() / 1024:.0f} Ko depuis le {start:%d/%m} "
            f"(remise à zéro auto le {_next_boundary(start, day):%d/%m})")
