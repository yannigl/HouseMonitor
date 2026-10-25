"""Armement à distance : /arm <cible> et /disarm <cible> (alias /unarm).

<cible> : all, le nom d'un groupe, un surnom, ou le nom d'un appareil (ou d'une entité) appartenant à un groupe.

config.json -> "arm": {"groups": {"netatmo": {"prefixes": ["camera.netatmo_"]},
                                  "cameras": {"entities": ["camera.arlo_terrasse"]}, ...}}
"nicknames": {"living room camera": "camera.living_room_camera", "motion sensor": "input_boolean.motion_sensor_armed"}
Un surnom désigne une ou plusieurs entités (un entity_id, ou une liste) ; il est cherché avant la recherche par nom.
Chaque groupe liste des entités (exactes, ou par préfixe d'entity_id). /arm appelle <domaine>.turn_on sur chacune,
/disarm <domaine>.turn_off. Les groupes sont la liste blanche : un nom d'appareil ne peut viser que des entités qui y
figurent. Seuls certains domaines sont permis : jamais sirène, lumière, serrure ni volet.

Durée : /arm all 30 (minutes), /disarm motion sensor 24h, /arm all 2d (m, h, d). À l'échéance, les entités reviennent à
l'état qu'elles avaient avant la commande, et un message Telegram prévient. Une nouvelle commande sur les mêmes entités
annule la minuterie précédente (l'état d'origine est conservé). Les minuteries survivent au redémarrage du listener
(arm_timers.json). Le Pi n'a pas d'horloge à pile : juste après un reboot, l'heure peut être fausse jusqu'à la synchro réseau.
"""
import json
import re
import threading
import time
import urllib.error
from datetime import datetime

import summary
from common import CFG, HERE, norm

ALLOWED = ("camera", "switch", "input_boolean", "automation")


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


RETRY_DELAY = 3  # s avant le 2e essai d'un appel en erreur 5xx ou réseau (turn_on/turn_off sont idempotents)


def call_service(domain, service, entity_ids):
    """Appelle un service HA, avec un 2e essai si l'erreur est interne (5xx) ou réseau.
    Retourne None si OK, "(2e essai)" si le 2e essai a réussi, sinon le message d'erreur."""
    err = _call_once(domain, service, entity_ids)
    if err and (err.startswith("HTTP 5") or err.startswith(("URLError", "OSError", "TimeoutError", "ConnectionError"))):
        time.sleep(RETRY_DELAY)
        err2 = _call_once(domain, service, entity_ids)
        return "(2e essai)" if err2 is None else err2
    return err


def _call_once(domain, service, entity_ids):
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
                   "docker logs --since 5m homeassistant, or Developer tools → Actions")
        return f"HTTP {e.code} {msg}".strip()
    except Exception as e:
        return f"{type(e).__name__}: {e}"


def usage():
    gs = groups()
    if not gs:
        return ("Aucun groupe d'armement configuré. Ajoutez un bloc « arm » dans config.json "
                "(voir « Armement » dans le README), puis relancez le listener.")
    states = {s["entity_id"]: summary.effective_state(s) for s in summary.states()}
    lines = ["🛡 /arm all · /arm <groupe> · /arm <surnom ou nom d'un appareil>", "   /disarm … (mêmes cibles, alias /unarm)",
             "   Durée : /disarm all 30 (min), 24h, 2d — retour automatique à l'état précédent", "", "Groupes :"]
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
    return "\n".join(lines) + pending_text()


TIMERS = HERE / "arm_timers.json"
LOCK = threading.Lock()  # le thread des minuteries et les commandes écrivent le même fichier
UNITS = {"": 60, "m": 60, "h": 3600, "d": 86400, "j": 86400}
MAX_SECONDS = 30 * 86400


def _load_timers():
    try:
        return json.loads(TIMERS.read_text())
    except (OSError, ValueError):
        return []


def _save_timers(timers):
    TIMERS.write_text(json.dumps(timers))


def fmt_duration(sec):
    sec = int(sec)
    if sec % 86400 == 0:
        return f"{sec // 86400} j"
    if sec % 3600 == 0:
        return f"{sec // 3600} h"
    return f"{max(1, round(sec / 60))} min"


def _release(ents):
    """Retire `ents` des minuteries en cours (une nouvelle commande les remplace). -> {entité: état d'origine} de celles qui
    étaient sous minuterie : leur état d'origine est antérieur à la commande temporaire précédente."""
    kept, origin = [], {}
    for t in _load_timers():
        for e in list(t["prev"]):
            if e in ents:
                origin[e] = t["prev"].pop(e)
        if t["prev"]:
            kept.append(t)
    _save_timers(kept)
    return origin


def _split_duration(which, resolves):
    """« all 30 » -> ("all", 1800). Un nombre sans unité est une durée en minutes, sauf si le texte entier est un nom connu
    (appareil « camera 2 »). `resolves(texte)` dit si un texte désigne une cible."""
    m = re.fullmatch(r"(.*\S)\s+(\d+)\s*([mhdj]?)", which.strip(), re.I)
    if not m or (not m.group(3) and resolves(which)):
        return which, None
    return m.group(1), int(m.group(2)) * UNITS[m.group(3).lower()]


def _select(which, gs, resolved, states, states_list):
    """-> [(libellé, entités OK, problèmes)] ou un message d'erreur."""
    key = norm(which)
    by_norm = {norm(n): n for n in gs}
    nicks = nicknames()
    if key == "all":
        return [(n, r[0], r[1]) for n, r in resolved.items()]
    if key in by_norm:
        r = resolved[by_norm[key]]
        return [(by_norm[key], r[0], r[1])]
    if key in nicks:  # un groupe de même nom l'emporterait ; sinon le surnom, avant la recherche par nom
        nick, ents = nicks[key]
        r = resolve({"entities": ents}, states)
        return [(nick, r[0], r[1])]
    found = find_device(which, states_list, resolved)
    return found if isinstance(found, str) else [found]


def act(arm, which):
    """/arm ou /disarm. `which` : "all", un groupe, un nom d'appareil, éventuellement suivi d'une durée, ou None (aide)."""
    gs = groups()
    if not which or not gs:
        return usage()
    states_list = summary.states()
    states = {s["entity_id"]: summary.effective_state(s) for s in states_list}
    resolved = {name: resolve(g, states) for name, g in gs.items()}
    which, seconds = _split_duration(
        which, lambda w: not isinstance(_select(w, gs, resolved, states, states_list), str))
    if seconds is not None and not 60 <= seconds <= MAX_SECONDS:
        return "Durée invalide : de 1 minute à 30 jours (ex. 30, 30m, 24h, 2d)."
    selection = _select(which, gs, resolved, states, states_list)
    if isinstance(selection, str):
        return selection

    service = "turn_on" if arm else "turn_off"
    results = []
    for label, ok, problems in selection:
        by_dom = {}
        for eid in ok:
            by_dom.setdefault(eid.split(".")[0], []).append(eid)
        failed, done, notes = [], [], []
        for dom, ents in by_dom.items():
            err = call_service(dom, service, ents)
            if err and err != "(2e essai)":
                failed.append(f"{dom}.{service} : {err}")
            else:
                done += ents
                if err:
                    notes.append(f"{dom}.{service} : réussi au 2e essai (le 1er avait échoué)")
        results.append((label, ok, done, problems, failed, notes))
    time.sleep(2)  # laisser HA et les caméras appliquer le changement avant de relire les états
    after = {s["entity_id"]: summary.effective_state(s) for s in summary.states()}
    out = [("🛡 Armement" if arm else "🔓 Désarmement") + f" : {which}"
           + (f" pour {fmt_duration(seconds)}" if seconds else "")]
    changed = [e for _, _, done, _, _, _ in results for e in done]
    with LOCK:
        origin = _release(set(changed))  # une nouvelle commande remplace les minuteries des mêmes entités
        if seconds and changed:
            prev = {e: origin.get(e, states.get(e, "?")) for e in changed}
            timers = _load_timers()
            timers.append({"label": which, "arm": arm, "seconds": seconds, "until": time.time() + seconds, "prev": prev})
            _save_timers(timers)
    for label, ok, done, problems, failed, notes in results:
        out.append(f"\n• {label} ({len(ok)} entité{'s' if len(ok) > 1 else ''})")
        out += [f"    {e} → {after.get(e, '?')}" for e in done]
        out += [f"    ❌ {f}" for f in failed] + [f"    ⚠️ {p}" for p in problems] + [f"    ↻ {n}" for n in notes]
    if seconds and changed:
        until = datetime.fromtimestamp(time.time() + seconds)
        when = f"{until:%Hh%M}" if seconds < 86400 else f"{until:%d/%m à %Hh%M}"
        out.append(f"\n⏱ Retour automatique à l'état précédent ({fmt_duration(seconds)}, à {when}) : "
                   + ", ".join(f"{e} → {prev[e]}" for e in changed))
    elif seconds:
        out.append("\n⏱ Aucune entité changée : pas de minuterie.")
    out.append("\nÉtats relus 2 s après l'ordre. Un appareil qui n'a pas changé d'état peut ne pas gérer ce service.")
    return "\n".join(out)


def _restore_one(timer):
    """Remet les entités d'une minuterie échue dans leur état d'origine. -> texte du message Telegram."""
    by_dom, skipped = {}, []
    for eid, state in timer["prev"].items():
        if state in ("unavailable", "unknown", "?"):
            skipped.append(f"{eid} (état d'origine « {state} »)")
        else:  # « off » = désarmé ; tout autre état d'une caméra (idle, streaming…) = armé
            by_dom.setdefault((eid.split(".")[0], "turn_off" if state == "off" else "turn_on"), []).append(eid)
    failed = []
    for (dom, service), ents in by_dom.items():
        err = call_service(dom, service, ents)
        if err and err != "(2e essai)":
            failed.append(f"{dom}.{service} : {err}")
    time.sleep(2)
    after = {s["entity_id"]: summary.effective_state(s) for s in summary.states()}
    kind = "armement" if timer["arm"] else "désarmement"
    lines = [f"⏱ Fin du {kind} temporaire ({fmt_duration(timer['seconds'])}) : {timer['label']}",
             "Retour à l'état précédent :"]
    lines += [f"    {e} → {after.get(e, '?')}  (avant : {timer['prev'][e]})" for e in timer["prev"] if e not in
              [x.split(" ")[0] for x in skipped]]
    lines += [f"    ❌ {f}" for f in failed] + [f"    ⚠️ non restauré : {x}" for x in skipped]
    return "\n".join(lines)


def pending_text():
    timers = _load_timers()
    if not timers:
        return ""
    lines = ["", "Minuteries en cours :"]
    for t in sorted(timers, key=lambda t: t["until"]):
        until = datetime.fromtimestamp(t["until"])
        lines.append(f"• {t['label']} ({'armé' if t['arm'] else 'désarmé'} {fmt_duration(t['seconds'])}) "
                     f"→ retour le {until:%d/%m à %Hh%M}")
    return "\n".join(lines)


def run_timers(poll=15):
    while True:
        try:
            with LOCK:
                timers = _load_timers()
                due = [t for t in timers if t["until"] <= time.time()]
                if due:
                    _save_timers([t for t in timers if t not in due])  # retirée d'abord : jamais restaurée deux fois
            for t in due:
                summary.send(_restore_one(t))
        except Exception as e:
            import sys
            print(f"minuteries KO : {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        time.sleep(poll)


def start_timers():
    threading.Thread(target=run_timers, daemon=True, name="arm-timers").start()
