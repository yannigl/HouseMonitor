#!/usr/bin/env python3
"""Résumé matinal Home Assistant -> Telegram (stdlib uniquement).

Découvre toutes les entités via /api/states, puis résume selon le type :
  sensor numérique  -> min / max (avec l'heure) / valeur actuelle
  binary_sensor     -> nombre d'alertes (passages à "on") + heures
  switch / light /
  input_boolean     -> nombre de cycles (on -> off) + heures de départ
"""
import json
import os
import sys
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

CFG = json.loads((Path(__file__).parent / "config.json").read_text())
HA = CFG.get("ha_url", "http://127.0.0.1:8123")
AUTH = {"Authorization": f"Bearer {CFG['ha_token']}"}
HOURS = CFG.get("hours", 24)
DOMAINS = CFG.get("domains", ["sensor", "binary_sensor", "switch", "light", "input_boolean"])
EXCLUDE = CFG.get("exclude", [])
MAX_TIMES = 5  # au-delà, on n'affiche plus la liste des heures
NETLOG = Path(__file__).parent / "netlog.csv"
TLS_OVERHEAD = 6000  # ~octets de handshake TLS par connexion (estimation)
NET = {"sent": 0, "recv": 0}  # octets internet (Telegram seulement, HA est en local)


def http(url, headers=None, data=None):
    req = urllib.request.Request(url, headers=headers or {}, data=data)
    with urllib.request.urlopen(req, timeout=60) as r:
        raw = r.read()
        if not url.startswith(HA):  # trafic internet : on compte
            NET["sent"] += len(data or b"") + len(str(req.header_items())) + 200
            NET["recv"] += len(raw) + len(str(r.headers))
        return json.loads(raw)


def ts(state):
    return datetime.fromisoformat(state.get("last_changed") or state["last_updated"]).astimezone()


def hm(dt):
    return f"{dt:%Hh%M}"


def discover():
    """Entités à résumer : {entity_id: (nom, unité)}."""
    out = {}
    for s in http(f"{HA}/api/states", AUTH):
        eid = s["entity_id"]
        dom = eid.split(".")[0]
        if dom not in DOMAINS or any(eid.startswith(p) for p in EXCLUDE):
            continue
        attrs = s.get("attributes", {})
        if dom == "sensor" and "unit_of_measurement" not in attrs:
            continue  # on ne garde que les capteurs numériques
        out[eid] = (attrs.get("friendly_name", eid), attrs.get("unit_of_measurement", ""))
    return out


def history(entity_ids):
    start = datetime.now(timezone.utc) - timedelta(hours=HOURS)
    qs = urllib.parse.urlencode(
        {"filter_entity_id": ",".join(entity_ids), "minimal_response": "", "no_attributes": ""}
    )
    data = http(f"{HA}/api/history/period/{start.isoformat()}?{qs}", AUTH)
    return {lst[0]["entity_id"]: lst for lst in data if lst}


def numeric_line(name, unit, states):
    pts = []
    for s in states:
        try:
            pts.append((ts(s), float(s["state"])))
        except (ValueError, KeyError):
            pass  # unavailable / unknown
    if not pts:
        return None
    lo = min(pts, key=lambda p: p[1])
    hi = max(pts, key=lambda p: p[1])
    if lo[1] == hi[1]:
        return f"{name} : {lo[1]:g}{unit} (stable)"
    return (f"{name} : {lo[1]:g}{unit} à {hm(lo[0])} → {hi[1]:g}{unit} à {hm(hi[0])}, "
            f"actuel {pts[-1][1]:g}{unit}")


def on_events(states):
    """Heures des passages à 'on' (le 1er état peut être un report antérieur)."""
    return [ts(s) for s in states[1:] if s["state"] == "on"]


def alert_line(name, states):
    ev = on_events(states)
    if not ev:
        return None
    times = ", ".join(hm(t) for t in ev) if len(ev) <= MAX_TIMES else f"dernière {hm(ev[-1])}"
    return f"{name} : {len(ev)} alerte{'s' if len(ev) > 1 else ''} ({times})"


def cycle_line(name, states):
    ev = on_events(states)
    cur = "allumé" if states and states[-1]["state"] == "on" else "éteint"
    if not ev:
        return None if cur == "éteint" else f"{name} : {cur}, aucun cycle"
    times = ", ".join(hm(t) for t in ev) if len(ev) <= MAX_TIMES else f"dernier {hm(ev[-1])}"
    return f"{name} : {len(ev)} cycle{'s' if len(ev) > 1 else ''} ({times}), {cur}"


def build():
    ents = discover()
    hist = history(list(ents))
    sections = defaultdict(list)
    for eid, (name, unit) in sorted(ents.items(), key=lambda kv: kv[1][0]):
        states = hist.get(eid, [])
        dom = eid.split(".")[0]
        if dom == "sensor":
            line = numeric_line(name, unit, states)
            key = "📈 Capteurs"
        elif dom == "binary_sensor":
            line = alert_line(name, states)
            key = "🚨 Alertes"
        else:
            line = cycle_line(name, states)
            key = "🔌 Switchs / lumières"
        if line:
            sections[key].append("• " + line)
    text = f"☀️ Résumé du {datetime.now():%d/%m} (dernières {HOURS} h)"
    for key, lines in sections.items():
        text += f"\n\n{key}\n" + "\n".join(lines)
    return text


def send(text):
    # Telegram limite à 4096 caractères par message
    for i in range(0, len(text), 4000):
        NET["sent"] += TLS_OVERHEAD
        http(
            f"https://api.telegram.org/bot{CFG['telegram_token']}/sendMessage",
            data=urllib.parse.urlencode(
                {"chat_id": CFG["telegram_chat_id"], "text": text[i:i + 4000]}
            ).encode(),
        )


def log_net():
    """Journal de la consommation internet : date, octets git pull, octets Telegram."""
    git_bytes = int(os.environ.get("GIT_BYTES", 0))  # fourni par run.sh
    tg_bytes = NET["sent"] + NET["recv"]
    new = not NETLOG.exists()
    with NETLOG.open("a") as f:
        if new:
            f.write("date,git_pull_octets,telegram_octets,total_octets\n")
        f.write(f"{datetime.now():%Y-%m-%d %H:%M},{git_bytes},{tg_bytes},{git_bytes + tg_bytes}\n")
    return git_bytes, tg_bytes


def net_line():
    """Conso cumulée du mois courant d'après netlog.csv."""
    if not NETLOG.exists():
        return ""
    month = f"{datetime.now():%Y-%m}"
    total = sum(int(l.split(",")[3]) for l in NETLOG.read_text().splitlines()[1:]
                if l.startswith(month))
    return f"\n\n📡 Data 4G du script ce mois : {total / 1024:.0f} Ko"


if __name__ == "__main__":
    msg = build() + net_line()
    if "--dry" in sys.argv:
        print(msg)
    else:
        send(msg)
        log_net()
