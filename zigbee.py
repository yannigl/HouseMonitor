"""Santé du réseau Zigbee (ZHA) : qualité des liens (LQI, RSSI), dernier contact, disponibilité sur la période, voisins et route vers le coordinateur.

Sources : commande WebSocket « zha/devices » de HA (la même que la visualisation du réseau) + historique de HA pour les coupures (availability.py).
Telegram : /zigbee [heures] (24 par défaut) · /zigbee debug (champs fournis par HA, si un chiffre manque) · /status zigbee.   Pi : python3 zigbee.py [heures] [debug]
Repères : LQI < 80 ou RSSI < -85 dBm = lien faible ; disponibilité < 98 % = coupures. Aucune commande n'est envoyée aux appareils (lecture seule)."""
import json
import sys
from datetime import datetime, timedelta, timezone

import availability
import hastats
import summary
from common import CFG

LQI_WEAK, RSSI_WEAK, AVAIL_WEAK = 80, -85, 98.0


def fetch():
    """Liste brute des appareils ZHA."""
    return hastats.command(summary.HA, CFG["ha_token"], {"type": "zha/devices"})


def label(d):
    return d.get("user_given_name") or d.get("name") or d.get("ieee") or "?"


def age_text(d):
    """« il y a 3 min » à partir de last_seen (texte ISO ou secondes), '' si inconnu."""
    v = d.get("last_seen") or d.get("last_seen_time")
    try:
        if isinstance(v, (int, float)):
            t = datetime.fromtimestamp(v, timezone.utc)
        else:
            t = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
        secs = (datetime.now(timezone.utc) - t).total_seconds()
    except (TypeError, ValueError):
        return ""
    if secs < 90:
        return "vu à l'instant"
    if secs < 5400:
        return f"vu il y a {secs / 60:.0f} min"
    if secs < 172800:
        return f"vu il y a {secs / 3600:.0f} h"
    return f"vu il y a {secs / 86400:.0f} j"


def kind(d):
    t = str(d.get("device_type") or "").lower()
    return "coordinateur" if "coord" in t else "routeur" if "router" in t else "capteur sur pile" if "end" in t else t or "?"


def neighbor_text(d, by_ieee):
    """Voisins que l'appareil entend, du meilleur LQI au moins bon (deux au plus)."""
    ns = [n for n in d.get("neighbors") or [] if isinstance(n, dict)]
    ns.sort(key=lambda n: -(n.get("lqi") or 0))
    out = []
    for n in ns[:2]:
        other = by_ieee.get(n.get("ieee"))
        out.append(f"{label(other) if other else n.get('ieee', '?')} (LQI {n.get('lqi', '?')})")
    return ", ".join(out)


def report(hours=24, debug=False):
    devices = fetch()
    if debug:
        sample = next((d for d in devices if "coord" not in str(d.get("device_type")).lower()), devices[0] if devices else {})
        shown = {k: (v if not isinstance(v, (list, dict)) else f"{type(v).__name__}({len(v)})") for k, v in sample.items()}
        first_n = (sample.get("neighbors") or [None])[0]
        first_r = (sample.get("routes") or [None])[0]
        return ("🔧 /zigbee debug : champs d'un appareil\n" + json.dumps(shown, ensure_ascii=False, indent=1, default=str)[:1800]
                + f"\n\nun voisin : {json.dumps(first_n, default=str)[:300]}\nune route : {json.dumps(first_r, default=str)[:300]}")
    by_ieee = {d.get("ieee"): d for d in devices}
    coord = next((d for d in devices if kind(d) == "coordinateur"), None)
    try:
        avail = availability.compute(hours)
    except Exception as e:
        print(f"disponibilité illisible : {type(e).__name__}: {e}", file=sys.stderr)
        avail = {}
    norm_avail = {summary.norm(k): v for k, v in avail.items()}
    heard = {}  # ieee -> LQI vu par le coordinateur
    if coord:
        for n in coord.get("neighbors") or []:
            if isinstance(n, dict) and n.get("ieee"):
                heard[n["ieee"]] = n.get("lqi")
    rows = []
    for d in devices:
        if d is coord:
            continue
        name = label(d)
        lqi, rssi = d.get("lqi"), d.get("rssi")
        av = norm_avail.get(summary.norm(name))
        score = (av["pct"] if av else 100.0)
        weak = ((isinstance(lqi, (int, float)) and lqi < LQI_WEAK) or (isinstance(rssi, (int, float)) and rssi < RSSI_WEAK)
                or (av is not None and av["pct"] < AVAIL_WEAK) or d.get("available") is False)
        parts = [f"{kind(d)}"]
        parts.append(f"LQI {lqi if lqi is not None else '?'} · RSSI {rssi if rssi is not None else '?'} dBm")
        if d.get("available") is False:
            parts.append("🔴 indisponible maintenant")
        if av:
            parts.append(f"dispo {av['pct']:.1f} %" + (f" ({av['cuts']} coupure{'s' if av['cuts'] > 1 else ''}, plus longue {av['longest'] / 60:.0f} min)" if av["cuts"] else ""))
        age = age_text(d)
        if age:
            parts.append(age)
        nb = neighbor_text(d, by_ieee)
        line = f"{'⚠️' if weak else '✅'} {name} — " + " · ".join(parts)
        if nb:
            line += f"\n    voisins : {nb}"
        if coord and d.get("ieee") not in heard and heard:
            line += "\n    ℹ️ pas dans la table du coordinateur : relié par un autre appareil (relais) ou lien direct trop faible pour y figurer"
        rows.append(((0 if weak else 1), score, lqi if isinstance(lqi, (int, float)) else 999, line))
    rows.sort(key=lambda r: r[:3])
    head = f"📡 Zigbee — {len(rows)} appareil(s), disponibilité sur {hours:g} h" + (f"\nCoordinateur : {label(coord)}" if coord else "")
    legend = f"\n\nRepères : LQI < {LQI_WEAK} ou RSSI < {RSSI_WEAK} dBm = lien faible ; dispo < {AVAIL_WEAK:g} % = coupures. LQI et RSSI sont vus par le coordinateur ; « voisins » = appareils entendus, pas le chemin réel."
    text = head + "\n\n" + "\n".join(r[3] for r in rows)
    if len(text) + len(legend) > 4000:
        text = text[:3900].rsplit("\n", 1)[0] + "\n…"
    return text + legend


if __name__ == "__main__":
    args = sys.argv[1:]
    hrs = next((float(a) for a in args if a.replace(".", "", 1).isdigit()), 24.0)
    print(report(hrs, debug="debug" in args))
