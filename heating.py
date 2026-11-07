"""/heating on|away : chauffage Netatmo (thermostat) sur la position normale ou « absent » ; utilisé aussi par /away et /home.

config.json -> "heating": {"entities": ["climate.living_room"]}
Les modes de Netatmo (programme, absent, hors-gel…) s'appliquent à la MAISON entière, pas à une pièce : il suffit d'une entité par
maison (la même commande envoyée à plusieurs pièces d'une maison multiplie les appels, et l'API de Netatmo limite leur nombre). Si vous
avez plusieurs maisons dans Netatmo, ne listez que celles qui doivent suivre /heating. Sans liste configurée, rien n'est actionné :
`/heating` affiche les thermostats trouvés et comment les ajouter.
  /heating on   -> position normale, celle du programme de la maison (preset « schedule » de HA)
  /heating away -> position « absent » (température plus basse, preset « away »)
Le preset est choisi parmi `preset_modes` de l'entité ; s'il n'y est pas, rien n'est envoyé et la liste disponible est affichée.
"""
import sys
import threading
import time

import arm
import summary
from common import CFG

MODES = {  # mot de la commande -> (libellé, presets acceptés par ordre de préférence)
    "on": ("normale", ("schedule", "home", "normal", "comfort")),
    "home": ("normale", ("schedule", "home", "normal", "comfort")),
    "normal": ("normale", ("schedule", "home", "normal", "comfort")),
    "away": ("absent", ("away",)),
}


CONFIRM_EVERY, CONFIRM_FOR = 5, 30  # s : relecture de l'état après l'ordre, puis suivi en arrière-plan
FOLLOW_EVERY, FOLLOW_FOR = 30, 15 * 60  # s : suivi : un message quand Netatmo confirme (ou n'a toujours pas confirmé)
WANTED = {}  # entity_id -> preset visé par le dernier ordre (un nouvel ordre arrête le suivi du précédent)


def _read(eid):
    return summary.http(f"{summary.HA}/api/states/{eid}", summary.AUTH)


def _has(s, preset):
    return (s.get("attributes", {}).get("preset_mode") or "").lower() == preset.lower()


def _follow(eid, preset, label, chat_id):
    """Après les 30 s d'attente de /heating : relit l'état toutes les 30 s (15 min au plus) et envoie un message à la confirmation de Netatmo."""
    deadline = time.time() + FOLLOW_FOR
    also = [chat_id] if chat_id is not None else None
    while time.time() < deadline:
        time.sleep(FOLLOW_EVERY)
        if WANTED.get(eid) != preset:
            return  # un autre ordre a suivi
        try:
            s = _read(eid)
        except Exception as e:
            print(f"suivi chauffage {eid} : {type(e).__name__}: {e}", file=sys.stderr, flush=True)
            continue
        if _has(s, preset):
            summary.send(f"✅ Chauffage confirmé par Netatmo : position {label}\n{_describe(s)}", also=also)
            return
    if WANTED.get(eid) == preset:
        summary.send(f"⚠️ Chauffage : {FOLLOW_FOR // 60} min après l'ordre « {label} », Netatmo n'a toujours pas confirmé ({eid}). "
                     "Vérifiez l'application Netatmo ; /heating montre l'état vu par HA.", also=also)


def configured():
    return list(CFG.get("heating", {}).get("entities", []))


def _climates():
    return [s for s in summary.states() if s["entity_id"].startswith("climate.")]


def _describe(s):
    a = s.get("attributes", {})
    bits = [f"preset « {a.get('preset_mode')} »" if a.get("preset_mode") else "", f"consigne {a['temperature']}°C" if a.get("temperature") is not None else "",
            f"mesure {a['current_temperature']}°C" if a.get("current_temperature") is not None else ""]
    return f"{a.get('friendly_name', s['entity_id'])} ({s['entity_id']}) : {s['state']}, " + ", ".join(b for b in bits if b)


def usage():
    cl = _climates()
    lines = ["🔥 /heating on (position normale / programme) · /heating away (absent, plus bas)", ""]
    ent = configured()
    if ent:
        lines.append("Thermostats suivis :")
        by_id = {s["entity_id"]: s for s in cl}
        lines += [f"• {_describe(by_id[e])}" if e in by_id else f"• {e} : introuvable" for e in ent]
    else:
        lines.append("Aucun thermostat configuré : ajoutez `\"heating\": {\"entities\": [\"climate.…\"]}` dans config.json (une entité par maison), puis /restart.")
    if cl:
        lines += ["", "Thermostats trouvés dans HA :"] + [f"• {_describe(s)}" for s in cl]
    return "\n".join(lines)


def act(mode, chat_id=None):
    """/heating <mode> : `mode` « on » ou « away » (ou None pour l'aide). Retourne le texte de réponse."""
    if mode is None or mode.lower() not in MODES:
        return usage()
    label, wanted = MODES[mode.lower()]
    ent = configured()
    if not ent:
        return usage()
    by_id = {s["entity_id"]: s for s in _climates()}
    out = [f"🔥 Chauffage → position {label}"]
    for eid in ent:
        s = by_id.get(eid)
        if not s:
            out.append(f"• {eid} : introuvable")
            continue
        a = s.get("attributes", {})
        modes = a.get("preset_modes") or []
        preset = next((m for w in wanted for m in modes if m.lower() == w), None)
        if s["state"] == "unavailable":
            out.append(f"• {eid} : indisponible, rien n'a été envoyé")
        elif not preset:
            out.append(f"• {eid} : pas de preset « {' / '.join(wanted)} » (disponibles : {', '.join(modes) or 'aucun'}), rien n'a été envoyé")
        elif a.get("preset_mode", "").lower() == preset.lower():
            out.append(f"• {_describe(s)} : déjà en position {label}")
        else:
            err = arm.call_service("climate", "set_preset_mode", [eid], {"preset_mode": preset})
            if err and err != "(2e essai)":
                out.append(f"• ❌ {eid} : {err}")
                continue
            WANTED[eid] = preset
            after, ok = s, False
            for _ in range(int(CONFIRM_FOR // CONFIRM_EVERY)):  # Netatmo répond par son cloud : relecture toutes les 5 s pendant 30 s
                time.sleep(CONFIRM_EVERY)
                try:
                    after = _read(eid)
                except Exception:
                    continue
                if _has(after, preset):
                    ok = True
                    break
            if ok:
                out.append(f"• ✅ {_describe(after)}")
            else:
                out.append(f"• ⏳ {_describe(after)}\n   ordre envoyé, pas encore confirmé après {CONFIRM_FOR} s : je vous écris dès que Netatmo confirme (15 min au plus)")
                threading.Thread(target=_follow, args=(eid, preset, label, chat_id), daemon=True, name="heating-follow").start()
    return "\n".join(out)
