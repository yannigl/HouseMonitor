"""Disponibilité des entités dans HA sur une période : combien de temps « unavailable », nombre de coupures, plus longue coupure.

Usage : python3 availability.py [heures=24] [mot ...]      (défaut : toutes les prises/switch ; un mot filtre sur le nom ou l'entity_id, ex. abri chambre2)
Utile pour juger la qualité d'une liaison Zigbee : une prise souvent « unavailable » est trop loin du coordinateur ou d'un routeur."""
import sys
from datetime import datetime, timedelta, timezone

import summary

BAD = ("unavailable", "unknown")


def main(argv):
    hours = int(argv[0]) if argv and argv[0].isdigit() else 24
    words = [summary.norm(a) for a in argv if not a.isdigit()]
    sts = summary.states()
    ents = {s["entity_id"]: (s.get("attributes") or {}).get("friendly_name", s["entity_id"]) for s in sts}
    if words:
        ids = [e for e, n in ents.items() if any(w in summary.norm(n) or w in e for w in words)]
    else:
        ids = [e for e in ents if e.startswith("switch.")]
    if not ids:
        sys.exit("Aucune entité trouvée.")
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=hours)
    hist = summary.history(ids, start, end)
    print(f"Disponibilité sur {hours} h (du {start.astimezone():%d/%m %Hh%M} au {end.astimezone():%d/%m %Hh%M})")
    print(f"{'entité':44} {'dispo':>7} {'coupures':>9} {'plus longue':>12}")
    rows = []
    for eid in ids:
        series = hist.get(eid, [])
        if not series:
            rows.append((0.0, f"{eid[:43]:44} {'pas de données':>7}"))
            continue
        pts = [(max(summary.ts(s), start), s["state"] in BAD) for s in series]
        down, cuts, longest, run_start = 0.0, 0, 0.0, None
        for i, (t, bad) in enumerate(pts):
            nxt = pts[i + 1][0] if i + 1 < len(pts) else end
            d = (nxt - t).total_seconds()
            if bad:
                down += d
                run_start = d if run_start is None else run_start + d
                longest = max(longest, run_start)
                if i == 0 or not pts[i - 1][1]:
                    cuts += 1
            else:
                run_start = None
        pct = 100 * (1 - down / (hours * 3600))
        rows.append((pct, f"{eid[:43]:44} {pct:6.1f}% {cuts:9d} {longest / 60:9.0f} min"))
    for _, line in sorted(rows):
        print(line)


if __name__ == "__main__":
    main(sys.argv[1:])
