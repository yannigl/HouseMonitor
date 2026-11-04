"""Exporte l'historique brut de HA (tous les relevés) des capteurs dont le nom contient un mot, en CSV.

Usage : python3 exportdata.py [heures=24] [mot ...]      (défaut : download upload)
Écrit export_<heures>h.csv (horodatage local, entité, nom, valeur, unité) et affiche le rythme des relevés."""
import statistics
import sys
from datetime import datetime, timedelta

import summary


def main(argv):
    hours = int(argv[0]) if argv and argv[0].isdigit() else 24
    words = [summary.norm(a) for a in argv if not a.isdigit()] or ["download", "upload"]
    sts = summary.states()
    ents = {eid: (n, u) for eid, (n, u) in summary.discover(sts).items() if any(w in summary.norm(n) for w in words)}
    if not ents:
        print("Aucun capteur trouvé pour :", ", ".join(words))
        return
    start = (datetime.now() - timedelta(hours=hours)).astimezone()
    hist = summary.history(list(ents), start, None)
    path = f"export_{hours}h.csv"
    with open(path, "w") as f:
        f.write("heure;entite;nom;valeur;unite\n")
        for eid, (name, unit) in ents.items():
            rows = hist.get(eid, [])
            for st in rows:
                f.write(f"{summary.ts(st).replace(tzinfo=None):%Y-%m-%d %H:%M:%S};{eid};{name};{st['state']};{unit}\n")
            t = [summary.ts(st) for st in rows]
            gaps = [(b - a).total_seconds() for a, b in zip(t, t[1:])]
            nums = [float(st["state"]) for st in rows if st["state"].replace(".", "", 1).replace("-", "", 1).isdigit()]
            print(f"{eid} [{unit}] : {len(rows)} relevés en {hours} h"
                  + (f", intervalle médian {statistics.median(gaps):.0f} s, min {min(gaps):.0f} s, max {max(gaps):.0f} s" if gaps else "")
                  + (f", valeurs {min(nums):g} → {max(nums):g}, {sum(1 for v in nums if v == 0)} à zéro" if nums else ""))
    print("Fichier :", path)


if __name__ == "__main__":
    main(sys.argv[1:])
