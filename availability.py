"""Disponibilité des appareils dans HA sur une période : temps passé « unavailable », nombre de coupures, plus longue coupure.

Usage : python3 availability.py [heures=24] [mot ...]      (sans mot : tous les appareils, les moins disponibles d'abord ; un mot filtre sur le nom de l'appareil
ou de l'entité, ex. abri chambre2). Les appareils sont désignés par leur nom dans HA (« Switch Abri »), avec l'entité mesurée.
Utile pour juger la qualité d'une liaison Zigbee : un appareil souvent « unavailable » est trop loin du coordinateur ou d'un routeur."""
import sys
from datetime import datetime, timedelta, timezone

import summary

PREFER = ("switch", "light", "climate", "cover", "lock", "fan", "binary_sensor", "sensor")  # entité représentant l'appareil (pas les boutons, toujours « unknown »)


def downtime(series, start, end):
    """(secondes « unavailable », coupures, plus longue coupure en s) ; seul « unavailable » compte : « unknown » est un état normal pour certains capteurs."""
    pts = [(max(summary.ts(s), start), s["state"] == "unavailable") for s in series]
    down, cuts, longest, run = 0.0, 0, 0.0, 0.0
    for i, (t, bad) in enumerate(pts):
        nxt = pts[i + 1][0] if i + 1 < len(pts) else end
        d = max(0.0, (nxt - t).total_seconds())
        if bad:
            down += d
            run += d
            longest = max(longest, run)
            if i == 0 or not pts[i - 1][1]:
                cuts += 1
        else:
            run = 0.0
    return down, cuts, longest


def compute(hours=24, words=()):
    """{nom d'appareil: {"pct", "cuts", "longest" (s), "eid"}} d'après l'historique de HA ; `words` filtre sur le nom de l'appareil ou de l'entité."""
    words = [summary.norm(w) for w in words]
    sts = summary.states()
    names = {s["entity_id"]: (s.get("attributes") or {}).get("friendly_name", s["entity_id"]) for s in sts}
    try:
        dmap = summary.devices_map()
    except Exception:
        dmap = {}
    groups = {}  # appareil -> [entity_id]
    for eid in names:
        info = dmap.get(eid) or {}
        key = info.get("d") or eid
        groups.setdefault(key, {"name": info.get("n") or names[eid], "ents": []})["ents"].append(eid)
    chosen = {}
    for key, g in groups.items():
        if words and not any(w in summary.norm(g["name"]) or any(w in e or w in summary.norm(names[e]) for e in g["ents"]) for w in words):
            continue
        for dom in PREFER:
            cand = [e for e in g["ents"] if e.startswith(dom + ".")]
            if cand:
                chosen[key] = (g["name"], sorted(cand)[0])
                break
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=hours)
    hist = summary.history([e for _, e in chosen.values()], start, end) if chosen else {}
    out = {}
    for name, eid in chosen.values():
        series = hist.get(eid, [])
        if series:
            down, cuts, longest = downtime(series, start, end)
            out[name] = {"pct": 100 * (1 - down / (hours * 3600)), "cuts": cuts, "longest": longest, "eid": eid}
    return out


def main(argv):
    hours = int(argv[0]) if argv and argv[0].isdigit() else 24
    words = [a for a in argv if not a.isdigit()]
    res = compute(hours, words)
    if not res:
        sys.exit("Aucun appareil trouvé" + (f" pour : {', '.join(words)}" if words else "") + ".")
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=hours)
    print(f"Disponibilité sur {hours} h (du {start.astimezone():%d/%m %Hh%M} au {end.astimezone():%d/%m %Hh%M}) — seul « unavailable » compte comme coupure")
    for name, r in sorted(res.items(), key=lambda kv: kv[1]["pct"]):
        print(f"{name[:32]:32} {r['pct']:6.1f}% {r['cuts']:5d} coupure(s), plus longue {r['longest'] / 60:4.0f} min   ({r['eid']})")


if __name__ == "__main__":
    main(sys.argv[1:])
