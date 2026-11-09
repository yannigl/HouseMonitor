"""Depuis quand un compteur cumulé (énergie d'une prise…) compte-t-il ? Date de ses premières statistiques à long terme dans HA, et sa valeur d'alors.

Usage : python3 firstseen.py [mot ...]      (défaut : tous les capteurs en kWh / Wh ; un mot filtre sur le nom ou l'entity_id, ex. arrosage)
Le compteur d'une prise est le sien : il part de sa mise en service ou de sa dernière remise à zéro, pas d'une date choisie par HA."""
import sys
from datetime import datetime, timedelta, timezone

import hastats
import summary
from common import CFG


def main(argv):
    words = [summary.norm(a) for a in argv]
    sts = [s for s in summary.states() if (s.get("attributes") or {}).get("unit_of_measurement") in ("kWh", "Wh", "MWh")]
    if words:
        sts = [s for s in sts if any(w in summary.norm(s["entity_id"] + " " + (s["attributes"].get("friendly_name") or "")) for w in words)]
    if not sts:
        sys.exit("Aucun compteur d'énergie trouvé.")
    ids = [s["entity_id"] for s in sts]
    start = datetime(2020, 1, 1, tzinfo=timezone.utc)
    res = hastats.command(summary.HA, CFG["ha_token"], {"type": "recorder/statistics_during_period", "start_time": start.isoformat(),
                                                        "end_time": datetime.now(timezone.utc).isoformat(), "statistic_ids": ids, "period": "month",
                                                        "types": ["sum", "state"]})
    for s in sts:
        eid = s["entity_id"]
        rows = res.get(eid) or []
        a = s["attributes"]
        print(f"\n{a.get('friendly_name', eid)} ({eid}) : actuel {s['state']} {a.get('unit_of_measurement')}")
        if a.get("last_reset"):
            print(f"   dernière remise à zéro déclarée : {a['last_reset']}")
        if rows:
            first = rows[0]["start"]
            first = datetime.fromtimestamp(first / 1000) if isinstance(first, (int, float)) else datetime.fromisoformat(first.replace("Z", "+00:00")).astimezone()
            print(f"   premières statistiques : {first:%d/%m/%Y}, valeur alors {rows[0].get('state')} (cumul {rows[0].get('sum')})")
        else:
            print("   pas de statistiques à long terme (HA ne garde que 10 jours de détail pour cette entité)")


if __name__ == "__main__":
    main(sys.argv[1:])
