"""/launch <nom>, /send <nom> [home] et /pause <nom> : pilote un aspirateur (Roomba…) du domaine vacuum de HA.

/launch : aspiration standard (vacuum.start) · /send : retour à la base (vacuum.return_to_base) · /pause (vacuum.pause).
Le nom est cherché dans le nom ou l'entity_id, sans casse ni accents ; s'il n'y a qu'un seul aspirateur, le nom est facultatif.
Ambigu -> rien n'est actionné.
"""
import time

import arm
import summary
from common import norm

ACTIONS = {
    "/launch": ("start", "lancé"),
    "/send": ("return_to_base", "renvoyé à sa base"),
    "/pause": ("pause", "mis en pause"),
}
STATE_FR = {"cleaning": "aspire", "docked": "sur sa base", "returning": "retourne à sa base", "paused": "en pause",
            "idle": "au repos", "error": "en erreur", "unavailable": "indisponible"}
EXPECTED = {"/launch": ("cleaning",), "/send": ("returning", "docked"), "/pause": ("paused", "idle")}


def _vacuums():
    return [s for s in summary.states() if s["entity_id"].startswith("vacuum.")]


def _name(s):
    return s.get("attributes", {}).get("friendly_name", s["entity_id"])


def usage():
    lines = ["🧹 /launch <nom> · /send <nom> home · /pause <nom>", ""]
    vs = _vacuums()
    if not vs:
        return lines[0] + "\nAucun aspirateur (vacuum.*) trouvé dans HA."
    lines.append("Aspirateurs :")
    lines += [f"• {_name(s)} — {s['entity_id']} ({STATE_FR.get(s['state'], s['state'])})" for s in vs]
    return "\n".join(lines)


def act(cmd, query):
    service, done = ACTIONS[cmd]
    words = norm(query or "").split()
    if cmd == "/send" and words and words[-1] in ("home", "base", "maison"):
        words.pop()  # « /send Roomba home » : le mot final n'est pas une partie du nom
    vs = _vacuums()
    if not vs:
        return usage()
    if words:
        vs = [s for s in vs if all(w in norm(_name(s) + " " + s["entity_id"]) for w in words)]
        if not vs:
            return f"Aucun aspirateur ne correspond à « {query} ».\n\n{usage()}"
    if len(vs) > 1:
        return ("Plusieurs aspirateurs : " + ", ".join(_name(s) for s in vs) +
                ". Précisez le nom, rien n'a été actionné.")
    s = vs[0]
    eid = s["entity_id"]
    if s["state"] == "unavailable":
        return f"⚠️ {_name(s)} ({eid}) est indisponible : rien n'a été actionné."
    err = arm.call_service("vacuum", service, [eid])
    if err and err != "(2e essai)":
        return f"❌ {_name(s)} ({eid}) : {err}"
    time.sleep(3)  # laisser le robot réagir avant de relire son état
    after = {x["entity_id"]: x["state"] for x in summary.states()}.get(eid, "?")
    ok = after in EXPECTED[cmd]
    text = f"{'✅' if ok else '⚠️'} {_name(s)} {done if ok else ''}".rstrip() + f" → {STATE_FR.get(after, after)}  ({eid})"
    if not ok:
        text += "\nL'état relu n'est pas celui attendu : le robot n'a peut-être pas encore répondu, vérifiez avec /update all."
    if err == "(2e essai)":
        text += "\n↻ réussi au 2e essai (le 1er avait échoué)"
    return text
