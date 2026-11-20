"""Coupures : /coupures [heures] reconstitue les arrêts du Pi, les démarrages de Home Assistant et les prises qui ont redémarré, et dit si la coupure
était générale (prise + Pi) ou limitée à une prise.

Trois sources :
• SIGNE DE VIE du listener (uptime_beat.json, toutes les `beat_seconds`, 120 par défaut) : au démarrage, si le Pi a redémarré depuis le dernier signe, l'arrêt est noté dans
  outages_log.json avec ses heures exactes et son caractère propre (arrêt demandé) ou brutal (coupure de courant, plantage). Disponible à partir de la mise en service de ce code ;
• JOURNAL DE HA (/api/logbook) : ses « started » / « stopped », même pour le passé ;
• COMPTEUR D'ÉNERGIE des prises (kWh / Wh) : une prise qui perd le courant revient à sa dernière valeur sauvegardée (38,39 → 38,36 kWh) : le compteur RECULE.
La date du retour de la prise est celle du premier relevé qui suit ; HA ne la voit que lorsque Zigbee est reparti, donc après son propre démarrage.
config.json -> "outages": {"beat_seconds": 120}"""
import json
import os
import sys
import threading
import time
import urllib.parse
from datetime import datetime, timedelta, timezone

import summary
from common import CFG, HERE
from switchrestore import boot_time

BEAT = HERE / "uptime_beat.json"  # {"t": dernier signe de vie, "boot": heure de démarrage du Pi, "clean": arrêt propre}
LOG = HERE / "outages_log.json"  # [{"down": dernier signe avant l'arrêt, "back": démarrage du Pi, "clean": arrêt demandé ?}]
EVERY = CFG.get("outages", {}).get("beat_seconds", 120)
MIN_DROP_KWH = 0.01  # une baisse plus petite est un arrondi, pas un redémarrage de prise


def _read(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write(path, data):
    try:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as e:
        print(f"{path.name} non écrit : {e}", file=sys.stderr)


def beat(clean=False):
    b = boot_time()
    _write(BEAT, {"t": time.time(), "boot": b.timestamp() if b else None, "clean": clean})


def mark_clean():
    """Appelé quand le listener s'arrête sur SIGTERM (arrêt ou redémarrage demandé au système) : le prochain démarrage saura que l'arrêt était propre."""
    beat(clean=True)


def on_start():
    """Au démarrage du listener : si le Pi a redémarré depuis le dernier signe de vie, note l'arrêt."""
    prev, b = _read(BEAT, None), boot_time()
    if prev and prev.get("boot") and b and abs(prev["boot"] - b.timestamp()) > 10:
        log = _read(LOG, [])
        log.append({"down": prev["t"], "back": b.timestamp(), "clean": bool(prev.get("clean"))})
        cut = time.time() - 90 * 86400
        _write(LOG, [e for e in log if e.get("back", 0) >= cut])
    beat()


def run():
    while True:
        time.sleep(EVERY)
        beat()


def start():
    on_start()
    if EVERY > 0:
        threading.Thread(target=run, daemon=True, name="uptime-beat").start()


def pi_downs(start, end):
    """[(arrêt, retour, propre)] depuis le signe de vie du listener."""
    out = []
    for e in _read(LOG, []):
        d, b = datetime.fromtimestamp(e["down"], timezone.utc), datetime.fromtimestamp(e["back"], timezone.utc)
        if b >= start and d <= end:
            out.append((d, b, e.get("clean", False)))
    return sorted(out)


def ha_events(start, end):
    """Journal de HA : -> ({"started": [datetime], "stopped": [datetime]}, erreur ou None)."""
    out = {"started": [], "stopped": []}
    try:
        data = summary.http(f"{summary.HA}/api/logbook/{start.isoformat()}?end_time={urllib.parse.quote(end.isoformat(), safe='')}", summary.AUTH)
        for e in data:
            msg = str(e.get("message") or "").lower()
            if e.get("domain") == "homeassistant" and ("started" in msg or "stopped" in msg):
                out["started" if "started" in msg else "stopped"].append(datetime.fromisoformat(e["when"]).astimezone(timezone.utc))
        return out, None
    except Exception as e:
        return out, f"{type(e).__name__}: {e}"


def plug_cuts(start, end):
    """Prises dont le compteur d'énergie a reculé : [{"name", "before", "back", "from", "to"}]. `before` : dernier relevé du compteur avant, `back` : premier après."""
    cur = {s["entity_id"]: s for s in summary.states()}
    try:
        dmap = summary.devices_map()
    except Exception:
        dmap = {}
    plugs = {(dmap.get(e) or {}).get("d") for e in cur if e.startswith("switch.")} - {None}
    counters = {e: s for e, s in cur.items() if e.startswith("sensor.") and s.get("attributes", {}).get("unit_of_measurement") in summary.UNIT_KWH
                and (dmap.get(e) or {}).get("d") in plugs and not summary.is_ghost(s)}
    if not counters:
        return []
    hist = summary.history(list(counters), start, end)
    cuts = []
    for e, s in counters.items():
        f = summary.UNIT_KWH[s["attributes"]["unit_of_measurement"]]
        rows = []
        for st in hist.get(e, []):
            try:
                rows.append((summary.ts(st).astimezone(timezone.utc), float(st["state"])))
            except (ValueError, KeyError, TypeError):
                pass
        name = (dmap.get(e) or {}).get("n") or s["attributes"].get("friendly_name", e)
        for (t0, a), (t1, b) in zip(rows, rows[1:]):
            if (a - b) * f >= MIN_DROP_KWH:
                cuts.append({"name": name, "before": t0, "back": t1, "from": a, "to": b, "unit": s["attributes"]["unit_of_measurement"]})
    return sorted(cuts, key=lambda c: c["back"])


def analyse(cut, ha, ha_error, downs, others):
    """Texte de conclusion pour une prise qui a redémarré : coupure générale (HA / Pi arrêté en même temps) ou prise seule."""
    back = cut["back"]
    near = lambda t: back - timedelta(minutes=10) <= t <= back + timedelta(minutes=2)
    for d, b, clean in downs:  # signe de vie du listener : exact
        if back - timedelta(minutes=15) <= b <= back + timedelta(minutes=2):
            if clean:
                return f"Pi arrêté proprement de {_h(d)} à {_h(b)} et la prise a perdu le courant en même temps : coupure volontaire (/shutdown) ou du secteur"
            return f"Pi arrêté de {_h(d)} à {_h(b)} sans arrêt propre : coupure générale, courant du Pi et de la prise"
    starts = [t for t in ha["started"] if near(t)]
    if starts:
        return f"HA a démarré à {_h(starts[0])}, au même moment : coupure générale (courant du Pi et de la prise), ou redémarrage du Pi"
    if ha_error:
        return "Home Assistant a-t-il redémarré en même temps ? Impossible à dire : journal de HA illisible (/coupures debug)"
    twin = [o for o in others if o is not cut and abs((o["back"] - back).total_seconds()) <= 600]
    if twin:
        return f"{len(twin) + 1} prises en même temps ({', '.join(sorted({o['name'] for o in twin}))}), HA n'a pas redémarré : coupure d'un circuit sans le Pi"
    return "HA n'a pas redémarré : la prise seule a perdu le courant (circuit, prise défaillante ou micro-coupure locale)"


def _h(t):
    return f"{t.astimezone():%d/%m %Hh%M}"


def report(hours=48.0, debug=False):
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=hours)
    ha, ha_error = ha_events(start, end)
    if debug:
        try:
            raw = summary.http(f"{summary.HA}/api/logbook/{(end - timedelta(hours=2)).isoformat()}?end_time={urllib.parse.quote(end.isoformat(), safe='')}", summary.AUTH)
        except Exception as e:
            return f"🔧 /coupures debug : journal de HA illisible ({type(e).__name__}: {e})"
        keys = sorted({k for e in raw for k in e})
        sample = [e for e in raw if e.get("domain") == "homeassistant"][:5] or raw[:3]
        return (f"🔧 /coupures debug : {len(raw)} entrées sur 2 h, champs : {', '.join(keys)}\n" + "\n".join(json.dumps(e, ensure_ascii=False)[:300] for e in sample)
                + f"\nsuivi du Pi : {BEAT.name} {'présent' if BEAT.exists() else 'absent'}, {len(_read(LOG, []))} arrêt(s) noté(s)")
    downs = pi_downs(start, end)
    cuts = plug_cuts(start, end)
    lines = [f"🔌 Coupures, {hours:g} dernières heures"]
    lines.append("\n🖥 Pi et Home Assistant")
    for d, b, clean in downs:
        lines.append(f"• Pi arrêté de {_h(d)} à {_h(b)} ({int((b - d).total_seconds() // 60)} min) : " + ("arrêt demandé" if clean else "brutal, coupure de courant ou plantage"))
    for t in ha["started"]:
        lines.append(f"• HA démarré à {_h(t)}" + (" (arrêt propre avant)" if any(0 <= (t - s).total_seconds() <= 900 for s in ha["stopped"]) else ""))
    if not downs and not ha["started"]:
        lines.append("• aucun arrêt relevé" + (f" (journal de HA illisible : {ha_error})" if ha_error else ""))
    lines.append("\n⚡ Prises (compteur d'énergie revenu en arrière = la prise a redémarré)")
    if not cuts:
        lines.append("• aucune prise n'a redémarré")
    for c in cuts:
        lines.append(f"• {c['name']} : revenue à {_h(c['back'])} (dernier relevé du compteur à {_h(c['before'])}), {c['from']:g} → {c['to']:g} {c['unit']}")
        lines.append(f"   → {analyse(c, ha, ha_error, downs, cuts)}")
    if not downs:
        lines.append("\nℹ️ Le suivi exact des arrêts du Pi (signe de vie du listener) n'enregistre que ce qui arrive depuis la mise en service de ce code.")
    return "\n".join(lines)


if __name__ == "__main__":  # python3 outages.py [heures]
    print(report(float(sys.argv[1]) if sys.argv[1:] else 48.0))
