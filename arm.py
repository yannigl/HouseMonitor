"""Armement à distance : /arm [all|<groupe>] et /disarm [all|<groupe>].

config.json -> "arm": {"groups": {"netatmo": {"prefixes": ["camera.netatmo_"]},
                                  "cameras": {"entities": ["camera.arlo_terrasse"]}, ...}}
Chaque groupe liste des entités (exactes, ou par préfixe d'entity_id). /arm appelle <domaine>.turn_on sur chacune,
/disarm <domaine>.turn_off. Seuls certains domaines sont permis : jamais sirène, lumière, serrure ni volet,
qu'une erreur de configuration ne doit pas pouvoir actionner.
"""
import json
import time
import urllib.error

import summary
from common import CFG

ALLOWED = ("camera", "switch", "input_boolean", "automation")


def groups():
    return CFG.get("arm", {}).get("groups", {})


def resolve(group, states):
    """-> (entités à actionner, problèmes) pour un groupe, d'après les états actuels de HA."""
    found, problems = [], []
    for eid in group.get("entities", []):
        if eid not in states:
            problems.append(f"{eid} : introuvable")
        elif eid not in found:
            found.append(eid)
    prefixes = group.get("prefixes", [])
    for eid in sorted(states):
        if prefixes and any(eid.startswith(p) for p in prefixes) and eid not in found:
            found.append(eid)
    ok = []
    for eid in found:
        dom = eid.split(".")[0]
        if dom not in ALLOWED:
            problems.append(f"{eid} : domaine « {dom} » non autorisé (permis : {', '.join(ALLOWED)})")
        elif states[eid] == "unavailable":
            problems.append(f"{eid} : indisponible")
        else:
            ok.append(eid)
    return ok, problems


def call_service(domain, service, entity_ids):
    """Appelle un service HA. Retourne None si OK, sinon le message d'erreur."""
    try:
        summary.http(f"{summary.HA}/api/services/{domain}/{service}",
                     {**summary.AUTH, "Content-Type": "application/json"},
                     json.dumps({"entity_id": entity_ids}).encode())
        return None
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read()).get("message", "")
        except Exception:
            msg = ""
        return f"HTTP {e.code} {msg}".strip()
    except Exception as e:
        return f"{type(e).__name__}: {e}"


def usage():
    gs = groups()
    if not gs:
        return ("Aucun groupe d'armement configuré. Ajoutez un bloc « arm » dans config.json "
                "(voir « Armement » dans le README), puis relancez le listener.")
    states = {s["entity_id"]: s["state"] for s in summary.states()}
    lines = ["🛡 Armement : /arm all, /arm <groupe>, /disarm all, /disarm <groupe>", "", "Groupes :"]
    for name, g in gs.items():
        ok, problems = resolve(g, states)
        lines.append(f"• {name} : {len(ok)} entité{'s' if len(ok) > 1 else ''}"
                     + (f", {len(problems)} problème{'s' if len(problems) > 1 else ''}" if problems else ""))
        lines += [f"    {e}" for e in ok] + [f"    ⚠️ {p}" for p in problems]
    return "\n".join(lines)


def act(arm, which):
    """/arm ou /disarm. `which` : "all", un nom de groupe, ou None (affiche l'aide)."""
    gs = groups()
    if not which or not gs:
        return usage()
    which = which.lower()
    if which != "all" and which not in gs:
        return f"Groupe inconnu « {which} ». Groupes : {', '.join(gs)}, all"
    service = "turn_on" if arm else "turn_off"
    states = {s["entity_id"]: s["state"] for s in summary.states()}
    todo, results = {}, []          # entité -> groupe ; lignes de résultat
    for name in (gs if which == "all" else [which]):
        ok, problems = resolve(gs[name], states)
        by_dom = {}
        for eid in ok:
            by_dom.setdefault(eid.split(".")[0], []).append(eid)
        failed = []
        for dom, ents in by_dom.items():
            err = call_service(dom, service, ents)
            if err:
                failed.append(f"{dom}.{service} : {err}")
            else:
                todo.update({e: name for e in ents})
        results.append((name, ok, problems, failed))
    time.sleep(2)  # laisser HA et les caméras appliquer le changement avant de relire les états
    after = {s["entity_id"]: s["state"] for s in summary.states()}
    out = [("🛡 Armement" if arm else "🔓 Désarmement") + f" : {which}"]
    for name, ok, problems, failed in results:
        out.append(f"\n• {name} ({len(ok)} entité{'s' if len(ok) > 1 else ''})")
        out += [f"    {e} → {after.get(e, '?')}" for e in ok if e in todo]
        out += [f"    ❌ {f}" for f in failed] + [f"    ⚠️ {p}" for p in problems]
    out.append("\nÉtats relus 2 s après l'ordre. Un appareil qui n'a pas changé d'état peut ne pas gérer ce service.")
    return "\n".join(out)
