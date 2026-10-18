"""Armement à distance : /arm <cible> et /unarm <cible> (alias /disarm).

<cible> : all, le nom d'un groupe, un surnom, ou le nom d'un appareil (ou d'une entité) appartenant à un groupe.

config.json -> "arm": {"groups": {"netatmo": {"prefixes": ["camera.netatmo_"]},
                                  "cameras": {"entities": ["camera.arlo_terrasse"]}, ...}}
"nicknames": {"living room camera": "camera.living_room_camera", "motion sensor": "input_boolean.motion_sensor_armed"}
Un surnom désigne une ou plusieurs entités (un entity_id, ou une liste) ; il est cherché avant la recherche par nom.
Chaque groupe liste des entités (exactes, ou par préfixe d'entity_id). /arm appelle <domaine>.turn_on sur chacune,
/unarm <domaine>.turn_off. Les groupes sont la liste blanche : un nom d'appareil ne peut viser que des entités qui y
figurent. Seuls certains domaines sont permis : jamais sirène, lumière, serrure ni volet.
"""
import json
import time
import unicodedata
import urllib.error

import summary
from common import CFG

ALLOWED = ("camera", "switch", "input_boolean", "automation")


def norm(s):
    """Minuscules sans accents, pour comparer « Caméra Salon » et « camera salon »."""
    return "".join(c for c in unicodedata.normalize("NFKD", str(s or "")) if not unicodedata.combining(c)).casefold().strip()


def groups():
    return CFG.get("arm", {}).get("groups", {})


def nicknames():
    """{surnom normalisé: (surnom, [entity_id])} d'après config.json -> arm.nicknames."""
    out = {}
    for nick, target in CFG.get("arm", {}).get("nicknames", {}).items():
        ents = [target] if isinstance(target, str) else [t for t in target if isinstance(t, str)] if isinstance(target, list) else []
        if ents:
            out[norm(nick)] = (nick, ents)
    return out


def resolve(group, states):
    """-> (entités à actionner, problèmes, toutes les entités autorisées du groupe, même indisponibles)."""
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
    members, ok = [], []
    for eid in found:
        dom = eid.split(".")[0]
        if dom not in ALLOWED:
            problems.append(f"{eid} : domaine « {dom} » non autorisé (permis : {', '.join(ALLOWED)})")
            continue
        members.append(eid)
        if states[eid] == "unavailable":
            problems.append(f"{eid} : indisponible")
        else:
            ok.append(eid)
    return ok, problems, members


def find_device(query, states_list, resolved):
    """Cherche <query> parmi les appareils/entités des groupes (jamais ailleurs).
    -> (libellé, entités OK, problèmes) ou un message d'erreur (aucun résultat, ou ambigu)."""
    allowed = sorted({e for _, _, members in resolved.values() for e in members})
    fnames = {s["entity_id"]: s.get("attributes", {}).get("friendly_name", s["entity_id"]) for s in states_list}
    states = {s["entity_id"]: s["state"] for s in states_list}
    try:
        dmap = summary.devices_map()
    except Exception:
        dmap = {}  # sans le registre d'appareils : recherche par nom d'entité seulement
    q = norm(query)
    cands = {}  # clé (appareil, ou l'entité seule) -> {"label", "ents", "exact"}
    for eid in allowed:
        info = dmap.get(eid) or {}
        names = [info.get("n"), fnames[eid], eid]
        if not any(q in norm(n) for n in names if n):
            continue
        c = cands.setdefault(info.get("d") or eid, {"label": info.get("n") or fnames[eid], "ents": [], "exact": False})
        c["ents"].append(eid)
        c["exact"] = c["exact"] or any(q == norm(n) for n in names if n)
    if not cands:
        nicks = [n for n, _ in nicknames().values()]
        return (f"Aucun appareil des groupes configurés ne correspond à « {query} ». "
                f"Groupes : {', '.join(groups())}, all." + (f" Surnoms : {', '.join(nicks)}." if nicks else "")
                + " (/arm seul liste les entités.)")
    if len(cands) > 1:
        exact = [c for c in cands.values() if c["exact"]]
        if len(exact) != 1:
            names = sorted(c["label"] for c in cands.values())
            return (f"« {query} » correspond à {len(cands)} appareils : {', '.join(names[:10])}"
                    f"{'…' if len(names) > 10 else ''}. Précisez le nom, rien n'a été actionné.")
        cands = {"x": exact[0]}
    c = next(iter(cands.values()))
    ok = [e for e in c["ents"] if states[e] != "unavailable"]
    return c["label"], ok, [f"{e} : indisponible" for e in c["ents"] if states[e] == "unavailable"]


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
        if e.code >= 500 and not msg:  # HA n'envoie pas de détail pour une erreur interne
            msg = ("erreur interne de Home Assistant (l'intégration a échoué) : "
                   "docker logs --since 5m homeassistant, ou Outils de développement → Actions")
        return f"HTTP {e.code} {msg}".strip()
    except Exception as e:
        return f"{type(e).__name__}: {e}"


def usage():
    gs = groups()
    if not gs:
        return ("Aucun groupe d'armement configuré. Ajoutez un bloc « arm » dans config.json "
                "(voir « Armement » dans le README), puis relancez le listener.")
    states = {s["entity_id"]: s["state"] for s in summary.states()}
    lines = ["🛡 /arm all · /arm <groupe> · /arm <surnom ou nom d'un appareil>", "   /unarm … (mêmes cibles)", "", "Groupes :"]
    for name, g in gs.items():
        ok, problems, _ = resolve(g, states)
        lines.append(f"• {name} : {len(ok)} entité{'s' if len(ok) > 1 else ''}"
                     + (f", {len(problems)} problème{'s' if len(problems) > 1 else ''}" if problems else ""))
        lines += [f"    {e}" for e in ok] + [f"    ⚠️ {p}" for p in problems]
    nicks = nicknames()
    if nicks:
        lines += ["", "Surnoms :"]
        for nick, ents in nicks.values():
            ok, problems, _ = resolve({"entities": ents}, states)
            lines.append(f"• {nick} → {', '.join(ents)}" + ("" if ok else "  ⚠️ " + "; ".join(p.split(' : ', 1)[-1] for p in problems)))
    return "\n".join(lines)


def act(arm, which):
    """/arm ou /unarm. `which` : "all", un groupe, un nom d'appareil, ou None (affiche l'aide)."""
    gs = groups()
    if not which or not gs:
        return usage()
    states_list = summary.states()
    states = {s["entity_id"]: s["state"] for s in states_list}
    resolved = {name: resolve(g, states) for name, g in gs.items()}
    key = norm(which)
    by_norm = {norm(n): n for n in gs}
    nicks = nicknames()
    if key == "all":
        selection = [(n, r[0], r[1]) for n, r in resolved.items()]
    elif key in by_norm:
        r = resolved[by_norm[key]]
        selection = [(by_norm[key], r[0], r[1])]
    elif key in nicks:  # un groupe de même nom l'emporterait ; sinon le surnom, avant la recherche par nom
        nick, ents = nicks[key]
        r = resolve({"entities": ents}, states)
        selection = [(nick, r[0], r[1])]
    else:
        found = find_device(which, states_list, resolved)
        if isinstance(found, str):
            return found
        selection = [found]

    service = "turn_on" if arm else "turn_off"
    results = []
    for label, ok, problems in selection:
        by_dom = {}
        for eid in ok:
            by_dom.setdefault(eid.split(".")[0], []).append(eid)
        failed, done = [], []
        for dom, ents in by_dom.items():
            err = call_service(dom, service, ents)
            if err:
                failed.append(f"{dom}.{service} : {err}")
            else:
                done += ents
        results.append((label, ok, done, problems, failed))
    time.sleep(2)  # laisser HA et les caméras appliquer le changement avant de relire les états
    after = {s["entity_id"]: s["state"] for s in summary.states()}
    out = [("🛡 Armement" if arm else "🔓 Désarmement") + f" : {which}"]
    for label, ok, done, problems, failed in results:
        out.append(f"\n• {label} ({len(ok)} entité{'s' if len(ok) > 1 else ''})")
        out += [f"    {e} → {after.get(e, '?')}" for e in done]
        out += [f"    ❌ {f}" for f in failed] + [f"    ⚠️ {p}" for p in problems]
    out.append("\nÉtats relus 2 s après l'ordre. Un appareil qui n'a pas changé d'état peut ne pas gérer ce service.")
    return "\n".join(out)
