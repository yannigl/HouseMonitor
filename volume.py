"""Volume du routeur 4G un jour donné, reconstruit à partir des débits (upload / download) enregistrés par HA, à comparer au relevé de l'opérateur.

Usage : python3 volume.py [jj/mm ou aaaa-mm-jj] [volume opérateur en Mo]     (défaut : hier)
• « relevés » : tous les relevés détaillés de HA (conservés 10 jours) : chaque débit compte jusqu'au relevé suivant (au plus 60 s).
• « statistiques » : moyenne horaire × 3600 s, depuis les statistiques à long terme de HA (conservées sans limite).
Affiche aussi le détail par heure pour voir quand le trafic a eu lieu."""
import sys
from datetime import datetime, timedelta

import summary
from common import CFG

RATE = {"B/s": 1, "KiB/s": 1024, "MiB/s": 1024 ** 2, "GiB/s": 1024 ** 3, "kB/s": 1000, "MB/s": 1000 ** 2, "kbit/s": 125, "Mbit/s": 125000}
MAX_HOLD = 60  # s : un relevé ne vaut pas plus longtemps que ça (trou de données)


def parse_day(arg):
    if not arg:
        return (datetime.now() - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    if arg.count("/") == 1:
        arg += f"/{datetime.now().year}"  # jj/mm : année en cours (strptime sans année est obsolète)
    for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(arg, fmt)
        except ValueError:
            pass
    sys.exit(f"Date illisible : {arg} (jj/mm ou aaaa-mm-jj)")


def from_samples(rows, start, end, factor):
    hours = [0.0] * 24
    pts = []
    for st in rows:
        try:
            pts.append((summary.ts(st).replace(tzinfo=None), float(st["state"])))
        except (ValueError, KeyError, TypeError):
            pts.append((summary.ts(st).replace(tzinfo=None), None))
    for i, (t, v) in enumerate(pts):
        nxt = pts[i + 1][0] if i + 1 < len(pts) else end
        t0, t1 = max(t, start), min(nxt, end, t + timedelta(seconds=MAX_HOLD))
        while v is not None and t0 < t1:  # coupe aux heures pleines
            cut = min(t1, (t0 + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0))
            hours[int((t0 - start).total_seconds() // 3600)] += v * factor * (cut - t0).total_seconds()
            t0 = cut
    return hours, sum(1 for t, _ in pts if start <= t < end)


def main(argv):
    start = parse_day(argv[0] if argv else None)
    end = start + timedelta(days=1)
    operator = float(argv[1]) if len(argv) > 1 else None
    ents = {eid: (n, u) for eid, (n, u) in summary.discover(summary.states()).items()
            if any(w in summary.norm(n) for w in ("download", "upload")) and u in RATE}
    if not ents:
        sys.exit("Aucun capteur de débit trouvé.")
    print(f"Journée du {start:%d/%m/%Y} (heure locale)")
    grand = {"relevés": 0.0, "statistiques": 0.0}
    for eid, (name, unit) in ents.items():
        f = RATE[unit]
        hist = summary.history([eid], (start - timedelta(minutes=5)).astimezone(), end.astimezone()).get(eid, [])
        hours, n = from_samples(hist, start, end, f)
        grand["relevés"] += sum(hours)
        print(f"\n{eid} [{unit}] : {n} relevés ce jour-là, volume {sum(hours) / 1e6:.1f} Mo")
        stats_hours = [0.0] * 24
        try:
            import hastats
            rows = hastats.statistics(summary.HA, CFG["ha_token"], [eid], start.astimezone(), end.astimezone(), "hour").get(eid, [])
            for t, mean, lo, hi in rows:
                if mean is not None and start <= t < end:
                    stats_hours[int((t - start).total_seconds() // 3600)] += mean * f * 3600
            grand["statistiques"] += sum(stats_hours)
            print(f"   statistiques horaires : {sum(stats_hours) / 1e6:.1f} Mo ({len(rows)} heures)")
        except Exception as e:
            print(f"   statistiques indisponibles : {type(e).__name__}: {e}")
        print("   heure  relevés(Mo)  stats(Mo)")
        for h in range(24):
            print(f"   {h:02d}h   {hours[h] / 1e6:10.2f}  {stats_hours[h] / 1e6:9.2f}")
    print()
    for k, v in grand.items():
        if v:
            print(f"TOTAL {k} (upload + download) : {v / 1e6:.1f} Mo" + (f" — {v / 1e6 / operator * 100:.1f} % de l'opérateur ({operator:g} Mo)" if operator else ""))


if __name__ == "__main__":
    main(sys.argv[1:])
