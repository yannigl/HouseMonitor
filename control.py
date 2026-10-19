"""/on <nom> et /off <nom> : allume ou éteint un interrupteur désigné par le nom de son appareil.

config.json -> "control": {"domains": ["switch"], "exclude": ["switch.prise_routeur", "switch.frigo"]}
  domains : types pilotables (par défaut switch ; permis : switch, light, input_boolean, fan).
  exclude : entity_id exacts ou préfixes à ne jamais piloter (prise du routeur, du congélateur…).
Le nom est cherché dans le nom de l'appareil, de l'entité et son entity_id, sans casse ni accents, dans n'importe quel
ordre des mots. S'il désigne plusieurs appareils, rien n'est actionné, sauf si l'un d'eux porte exactement ce nom.
"""
import time

import arm
import summary
from common import CFG, norm

SAFE_DOMAINS = ("switch", "light", "input_boolean", "fan")


def domains():
    wanted = CFG.get("control", {}).get("domains", ["switch"])
    return [d for d in wanted if d in SAFE_DOMAINS] or ["switch"]


def excluded(eid):
    return any(eid == x or eid.startswith(x) for x in CFG.get("control", {}).get("exclude", []))


def _groups(states_list):
    """Interrupteurs pilotables regroupés par appareil : clé -> {"label", "ents", "fnames", "states"}."""
    try:
        dmap = summary.devices_map()
    except Exception:
        dmap = {}  # sans registre d'appareils : chaque entité est son propre appareil
    doms = domains()
    groups = {}
    for s in states_list:
        eid = s["entity_id"]
        if eid.split(".")[0] not in doms or s.get("attributes", {}).get("restored"):
            continue
        info = dmap.get(eid) or {}
        fn = s.get("attributes", {}).get("friendly_name", eid)
        g = groups.setdefault(info.get("d") or eid, {"label": info.get("n") or fn, "ents": [], "fnames": {}, "states": {}})
        g["ents"].append(eid)
        g["fnames"][eid] = fn
        g["states"][eid] = s["state"]
    return groups


def _pick_entity(group, query_n):
    """L'entité à actionner dans un appareil : celle dont le nom est exactement la requête, sinon l'interrupteur principal."""
    ents = sorted(group["ents"])
    for e in ents:
        if query_n in (norm(group["fnames"][e]), norm(e)):
            return e
    switches = [e for e in ents if e.startswith("switch.")]
    return summary.primary_switch(group["label"], switches, group["fnames"]) if switches else ents[0]


def usage(on):
    groups = _groups(summary.states())
    lines = [f"💡 /on <nom> · /off <nom>   (domaines pilotables : {', '.join(domains())})", ""]
    if not groups:
        return lines[0] + "\nAucun interrupteur trouvé."
    lines.append("Appareils :")
    for g in sorted(groups.values(), key=lambda g: g["label"].lower()):
        e = _pick_entity(g, "")
        tag = " (exclu)" if excluded(e) else " (indisponible)" if g["states"][e] == "unavailable" else ""
        lines.append(f"• {g['label']} — {e}{tag}")
    return "\n".join(lines)


def act(on, query):
    """/on ou /off. `query` : le nom de l'appareil, ou None (affiche les appareils pilotables)."""
    if not query:
        return usage(on)
    states_list = summary.states()
    groups = _groups(states_list)
    q = norm(query)
    tokens = q.split()
    matches = {}
    for key, g in groups.items():
        blob = norm(" ".join([g["label"]] + list(g["fnames"].values()) + g["ents"]))
        if all(t in blob for t in tokens):
            matches[key] = g
    if not matches:
        return f"Aucun interrupteur ne correspond à « {query} ». (/on seul liste les appareils pilotables.)"
    if len(matches) > 1:
        exact = [g for g in matches.values()
                 if q == norm(g["label"]) or any(q in (norm(f), norm(e)) for e, f in g["fnames"].items())]
        if len(exact) != 1:
            names = sorted(g["label"] for g in matches.values())
            return (f"« {query} » correspond à {len(matches)} appareils : {', '.join(names[:10])}"
                    f"{'…' if len(names) > 10 else ''}. Précisez le nom, rien n'a été actionné.")
        matches = {"x": exact[0]}
    g = next(iter(matches.values()))
    eid = _pick_entity(g, q)
    others = [e for e in sorted(g["ents"]) if e != eid]
    if excluded(eid):
        return f"⛔ {g['label']} ({eid}) est exclu du pilotage par la configuration (control.exclude)."
    if g["states"][eid] == "unavailable":
        return f"⚠️ {g['label']} ({eid}) est indisponible : rien n'a été actionné."

    want = "on" if on else "off"
    err = arm.call_service(eid.split(".")[0], "turn_on" if on else "turn_off", [eid])
    if err and err != "(2e essai)":
        return f"❌ {g['label']} ({eid}) : {err}"
    time.sleep(2)  # laisser l'appareil appliquer l'ordre avant de relire son état
    after = {s["entity_id"]: s["state"] for s in summary.states()}.get(eid, "?")
    ok = after == want
    text = f"{'✅' if ok else '⚠️'} {g['label']} → {after}  ({eid})"
    if not ok:
        text += f"\nL'état relu n'est pas « {want} » : l'appareil n'a peut-être pas encore répondu, vérifiez avec /update all."
    if err == "(2e essai)":
        text += "\n↻ réussi au 2e essai (le 1er avait échoué)"
    if others:
        text += f"\nAutres interrupteurs de l'appareil, non touchés : {', '.join(others)}"
    return text
