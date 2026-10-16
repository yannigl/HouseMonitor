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


def pi_cum():
    """Cumul des octets internet du Pi (voir netcount.py), None si le compteur n'est pas installé."""
    try:
        import netcount
        return netcount.sample()
    except Exception:
        return None


def cycle():
    """(début du cycle, jour d'ancrage, cumul du Pi au début du cycle). Par défaut : le 1er du mois courant.
    Le cycle avance tout seul d'un mois quand sa date de fin est passée."""
    now = datetime.now()
    try:
        c = json.loads(CYCLE_FILE.read_text())
        start, day, base = datetime.fromisoformat(c["start"]), int(c["day"]), c.get("pi_base") or 0
    except (OSError, ValueError, KeyError):
        start, day, base = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0), 1, 0
    rolled = False
    while now >= _next_boundary(start, day):
        start, rolled = _next_boundary(start, day), True
    if rolled:
        base = pi_cum() or base
    if rolled or not CYCLE_FILE.exists():
        CYCLE_FILE.write_text(json.dumps({"start": start.isoformat(), "day": day, "pi_base": base}))
    return start, day, base


def reset_cycle():
    """/data reset : le compteur repart de 0 maintenant, prochain reset le même jour du mois suivant."""
    now = datetime.now()
    CYCLE_FILE.write_text(json.dumps({"start": now.isoformat(timespec="seconds"), "day": now.day,
                                      "pi_base": pi_cum() or 0}))


def cycle_total():
    """Octets internet du script sur le cycle : netlog.csv depuis le début + compteur du process en cours."""
    start = cycle()[0]
    since = f"{start:%Y-%m-%d %H:%M:%S}"
    total = NET["sent"] + NET["recv"]
    if NETLOG.exists():
        total += sum(int(l.split(",")[-1]) for l in NETLOG.read_text().splitlines()[1:]
                     if l.split(",")[0] > since)
    return total


def fmt_bytes(n):
    n = float(n)
    if n < 1024:
        return f"{n:.0f} o"
    if n < 1024 ** 2:
        return f"{n / 1024:.0f} Ko"
    if n < 1024 ** 3:
        return f"{n / 1024 ** 2:.1f} Mo".replace(".", ",")
    return f"{n / 1024 ** 3:.2f} Go".replace(".", ",")


def cycle_info():
    """Chiffres du cycle : début, prochain reset, octets du script, octets du Pi (None si non mesuré)."""
    start, day, base = cycle()
    cum = pi_cum()
    return {"start": start, "next": _next_boundary(start, day), "script": cycle_total(),
            "pi": None if cum is None else max(0, cum - base)}


def data_block():
    """Bloc affiché par /data et dans le message du matin."""
    i = cycle_info()
    lines = [f"📡 Data 4G depuis le {i['start']:%d/%m} (remise à zéro auto le {i['next']:%d/%m})",
             f"• Script HouseMonitor : {fmt_bytes(i['script'])}"]
    if i["pi"] is None:
        lines.append("• Pi au total : non mesuré (sudo ./setup-netcount.sh)")
    else:
        lines += [f"• Pi au total : {fmt_bytes(i['pi'])}",
                  f"• dont HA, système… (hors script) : {fmt_bytes(max(0, i['pi'] - i['script']))}"]
    return "\n".join(lines)


def data_compact():
    """Une ligne, pour /status et la confirmation de /data reset."""
    i = cycle_info()
    pi = "Pi non mesuré" if i["pi"] is None else f"Pi {fmt_bytes(i['pi'])}"
    return f"📡 Data 4G depuis le {i['start']:%d/%m} : script {fmt_bytes(i['script'])} · {pi}"
