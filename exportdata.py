"""Exporte l'historique brut de HA (tous les relevés) des capteurs dont le nom contient un mot, en CSV.

Sur le Pi : python3 exportdata.py [heures=24] [mot ...]      (défaut : download upload ; un capteur qui contient UN des mots)
Écrit export_<heures>h.csv (horodatage local, entité, nom, valeur, unité) et affiche le rythme des relevés.
Telegram  : /export [heures] <mot> [mot ...]   (le listener envoie le CSV en pièce jointe ; un capteur doit contenir TOUS les mots)"""
import re
import statistics
import sys
from datetime import datetime, timedelta

import summary

MAX_ENTITIES = 12  # par Telegram : au-delà, on demande de préciser (le Pi est sur une 4G à data comptée)
MAX_BYTES = 8_000_000
KEEP_DAYS_NOTE = 9.5 * 24  # HA ne garde l'historique détaillé que 10 jours par défaut (recorder: purge_keep_days)


def parse_args(args):
    """['48', 'dioxyde'] -> (48.0, ['dioxyde']). Une durée : « 48 » ou « 48h » (heures), « 2j » ou « 2d » (jours). 24 h par défaut."""
    hours, words = 24.0, []
    for a in args:
        m = re.fullmatch(r"(\d+(?:[.,]\d+)?)\s*([hjd]?)", a.strip().lower())
        if m:
            hours = float(m.group(1).replace(",", ".")) * (24 if m.group(2) in ("j", "d") else 1)
        elif a.strip():
            words.append(a.strip())
    return hours, words


def filename(hours, words):
    tag = "_".join(re.sub(r"[^a-z0-9]+", "", summary.norm(w)) for w in words[:3] if w) or "export"
    return f"export_{tag}_{hours:g}h.csv"


def entities(sts, words, match_all=False):
    """Capteurs dont le nom, l'entity_id ou l'appareil contient un mot (ou tous, avec `match_all`) ; mots français courants acceptés (dioxyde, pluie…)."""
    try:
        dmap = summary.devices_map()
    except Exception:
        dmap = {}
    out = {}
    for eid, (n, u) in summary.discover(sts).items():
        blob = summary.norm(" ".join(x for x in (eid, n, (dmap.get(eid) or {}).get("n")) if x))
        hits = [summary.norm(w) in blob or summary.ALIASES.get(summary.norm(w), summary.norm(w)) in blob for w in words]
        if hits and (all(hits) if match_all else any(hits)):
            out[eid] = (n, u)
    return out


def build(hours, words, match_all=False, limit=None):
    """-> (texte CSV ou None, lignes de rapport). Sans CSV, le rapport explique pourquoi. `limit` : nombre maximal de capteurs."""
    words = words or ["download", "upload"]
    ents = entities(summary.states(), words, match_all)
    if not ents:
        return None, [f"Aucun capteur trouvé pour : {' '.join(words)}  (essayez /list {words[0]})"]
    if limit and len(ents) > limit:
        names = ", ".join(sorted(n for n, _ in ents.values())[:6])
        return None, [f"{len(ents)} capteurs correspondent à « {' '.join(words)} » (au plus {limit}) : précisez avec un mot de plus.", f"Ex. : {names}…"]
    start = (datetime.now() - timedelta(hours=hours)).astimezone()
    hist = summary.history(list(ents), start, None)
    lines = ["heure;entite;nom;valeur;unite"]
    report, total = [], 0
    for eid, (name, unit) in ents.items():
        rows = hist.get(eid, [])
        total += len(rows)
        for st in rows:
            lines.append(f"{summary.ts(st).replace(tzinfo=None):%Y-%m-%d %H:%M:%S};{eid};{name};{st['state']};{unit}")
        t = [summary.ts(st) for st in rows]
        gaps = [(b - a).total_seconds() for a, b in zip(t, t[1:])]
        nums = summary.floats(rows)
        report.append(f"{name} ({eid}) [{unit}] : {len(rows)} relevés en {hours:g} h"
                      + (f", intervalle médian {statistics.median(gaps):.0f} s, min {min(gaps):.0f} s, max {max(gaps):.0f} s" if gaps else "")
                      + (f", valeurs {min(nums):g} → {max(nums):g}, {sum(1 for v in nums if v == 0)} à zéro" if nums else ""))
    csv = "\n".join(lines) + "\n"
    if limit and len(csv) > MAX_BYTES:
        return None, [f"Export trop gros ({len(csv) // 1000} ko, {total} relevés) : réduisez la durée ou précisez le mot."]
    head = f"📄 {len(ents)} capteur{'s' if len(ents) > 1 else ''}, {total} relevés sur {hours:g} h"
    if hours > KEEP_DAYS_NOTE:
        head += " (HA ne conserve que 10 jours de relevés détaillés)"
    return csv, [head, *report]


def main(argv):
    hours, words = parse_args(argv)
    csv, report = build(hours, words)
    if csv is None:
        print(*report, sep="\n")
        return
    path = f"export_{hours:g}h.csv"
    with open(path, "w", encoding="utf-8") as f:
        f.write(csv)
    print(*report[1:], sep="\n")
    print("Fichier :", path)


if __name__ == "__main__":
    main(sys.argv[1:])
