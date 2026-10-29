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
            time.sleep(3)  # laisser Netatmo appliquer le changement avant de relire l'état
            after = {x["entity_id"]: x for x in _climates()}.get(eid, s)
            ok = (after.get("attributes", {}).get("preset_mode") or "").lower() == preset.lower()
            out.append(f"• {'✅' if ok else '⚠️'} {_describe(after)}" + ("" if ok else " (le thermostat n'a pas encore confirmé)"))
    return "\n".join(out)
