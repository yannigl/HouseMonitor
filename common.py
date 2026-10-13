"""Partagé entre summary.py et listener.py : config, état, compteur data, heartbeat."""
import json
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).parent
CFG = json.loads((HERE / "config.json").read_text())
STATE_FILE = HERE / "state.json"
NETLOG = HERE / "netlog.csv"
HC_URL = CFG.get("healthchecks_url", "")
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


def heartbeat(ok=True):
    """Ping Healthchecks.io ; /fail déclenche une alerte immédiate, aucun ping -> alerte après la grâce."""
    if not HC_URL or not load_state()["ping"]:
        return False
    url = HC_URL if ok else HC_URL.rstrip("/") + "/fail"
    try:
        req = urllib.request.Request(url, data=b"", headers={"User-Agent": "housemonitor"})
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
        f.write(f"{datetime.now():%Y-%m-%d %H:%M},{source},{git_bytes},{tg},{git_bytes + tg}\n")
    NET["sent"] = NET["recv"] = 0


def month_total():
    """Octets internet cumulés ce mois d'après netlog.csv."""
    if not NETLOG.exists():
        return 0
    month = f"{datetime.now():%Y-%m}"
    return sum(int(l.split(",")[-1]) for l in NETLOG.read_text().splitlines()[1:]
               if l.startswith(month))
