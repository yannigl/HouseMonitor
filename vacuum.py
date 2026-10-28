"""/launch <nom>, /send <nom> [home] et /pause <nom> : pilote un aspirateur (Roomba…) du domaine vacuum de HA.

/launch : aspiration standard (vacuum.start) · /send : retour à la base (vacuum.return_to_base) · /pause (vacuum.pause).
Le nom est cherché dans le nom ou l'entity_id, sans casse ni accents ; s'il n'y a qu'un seul aspirateur, le nom est facultatif.
Ambigu -> rien n'est actionné.
"""
import threading
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


def act(cmd, query, chat_id=None):
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
    idle = {"/launch": ("cleaning",), "/send": ("docked", "returning"), "/pause": ("docked", "idle", "paused")}
    if s["state"] in idle[cmd]:  # rien à faire : ne pas déranger le robot (ni attendre 15 s une réponse qui ne viendra pas)
        return f"ℹ️ {_name(s)} est déjà « {STATE_FR.get(s['state'], s['state'])} » : rien à faire."
    err = arm.call_service("vacuum", service, [eid])
    if err and err != "(2e essai)":
        return f"❌ {_name(s)} ({eid}) : {err}"
    attrs = s.get("attributes", {})
    after = "?"
    for _ in range(5):  # un robot sur sa base met quelques secondes à partir : on relit jusqu'à 15 s
        time.sleep(3)
        try:
            after, attrs = _read(eid)
        except Exception:
            continue
        if after in EXPECTED[cmd]:
            break
    ok = after in EXPECTED[cmd]
    text = f"{'✅' if ok else '⚠️'} {_name(s)} {done if ok else ''}".rstrip() + f" → {STATE_FR.get(after, after)}  ({eid})"
    if not ok:
        text += ("\nL'état relu n'est pas celui attendu après 15 s : le robot n'a pas répondu (ou refuse l'ordre). "
                 "Vérifiez avec /update all.")
        text += "".join(f"\n{l}" for l in details(attrs))
    if err == "(2e essai)":
        text += "\n↻ réussi au 2e essai (le 1er avait échoué)"
    if cmd == "/launch":
        watch(eid, _name(s), chat_id)
        text += "\n👀 Je vous préviens ici en cas d'erreur, de bac plein et à la fin du ménage."
    return text


# --- suivi après /launch -----------------------------------------------------------------------------------------------
WATCH_EVERY = 20          # s entre deux lectures de HA (local : aucune data 4G)
WATCH_MAX = 4 * 3600      # s : on arrête de suivre un robot qui ne finit jamais
NOT_STARTED_AFTER = 300   # s : prévenir si le robot n'est toujours pas parti
WATCHING = {}             # un seul suivi par aspirateur : entity_id -> chats où écrire en plus du propriétaire


def _read(eid):
    s = summary.http(f"{summary.HA}/api/states/{eid}", summary.AUTH)
    return s["state"], s.get("attributes", {})


def _error(attrs):
    err = str(attrs.get("error") or "").strip()
    return "" if err.lower() in ("", "none", "0", "no error", "unknown") else err


def details(attrs):
    """Lignes lisibles tirées des attributs du robot (celles qui existent seulement)."""
    lines = []
    if attrs.get("bin_present") is False:
        lines.append("🗑 Bac absent ⚠️")
    elif attrs.get("bin_full") is True:
        lines.append("🗑 Bac plein ⚠️")
    elif attrs.get("bin_full") is False:
        lines.append("🗑 Bac : ok")
    if _error(attrs):
        lines.append(f"⚠️ Erreur : {_error(attrs)}")
    if attrs.get("cleaning_time") not in (None, ""):
        lines.append(f"⏱ Durée de nettoyage : {attrs['cleaning_time']} min")
    if attrs.get("cleaned_area") not in (None, ""):
        lines.append(f"📐 Surface : {attrs['cleaned_area']} m²")
    if attrs.get("battery_level") not in (None, ""):
        lines.append(f"🔋 Batterie : {attrs['battery_level']} %")
    return lines


def _say(eid, text):
    """Message de suivi : au propriétaire, et au chat d'où le robot a été lancé (un groupe, par exemple)."""
    summary.send(text, also=sorted(WATCHING.get(eid, ())))


def _watch(eid, name):
    """Après /launch : prévient par Telegram du démarrage manqué, d'une erreur, du bac plein et de la fin du ménage."""
    try:
        started, flagged, t0 = False, set(), time.time()
        while time.time() - t0 < WATCH_MAX:
            time.sleep(WATCH_EVERY)
            try:
                state, attrs = _read(eid)
            except Exception:
                continue  # HA injoignable un instant : on réessaie
            if state == "cleaning":
                started = True
            if not started and time.time() - t0 > NOT_STARTED_AFTER:
                _say(eid, f"⚠️ {name} n'a pas démarré (état : {STATE_FR.get(state, state)})\n" + "\n".join(details(attrs)))
                return
            err = _error(attrs)
            if err and ("error", err) not in flagged:
                flagged.add(("error", err))
                _say(eid, f"⚠️ {name} : erreur « {err} »")
            if attrs.get("bin_full") is True and "bin" not in flagged:
                flagged.add("bin")
                _say(eid, f"🗑 {name} : le bac est plein, il faut le vider.")
            if started and state in ("docked", "idle"):
                _say(eid, f"🧹 {name} a terminé (état : {STATE_FR.get(state, state)})\n" + "\n".join(details(attrs)))
                return
        _say(eid, f"⚠️ {name} : toujours pas terminé après {WATCH_MAX // 3600} h, je cesse de le suivre.")
    except Exception as e:
        print(f"suivi aspirateur KO : {type(e).__name__}: {e}", flush=True)
    finally:
        WATCHING.pop(eid, None)


def watch(eid, name, chat_id=None):
    if eid in WATCHING:  # déjà suivi : on ajoute seulement le chat de cette commande
        if chat_id is not None:
            WATCHING[eid].add(chat_id)
        return
    WATCHING[eid] = {chat_id} if chat_id is not None else set()
    threading.Thread(target=_watch, args=(eid, name), daemon=True, name="vacuum-watch").start()
