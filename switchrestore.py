"""Remet en marche les prises qui étaient allumées avant une coupure de courant (ou de liaison Zigbee) et qui sont revenues éteintes.

Principe : l'historique de HA (10 jours) montre « on → unavailable → off » quand une prise perd son alimentation puis revient avec son réglage d'usine
(éteinte). Une prise éteinte volontairement passe directement de « on » à « off », sans indisponibilité : elle n'est jamais touchée.
Pas d'instantané à tenir : un instantané pris après la coupure écraserait justement l'information utile.

config.json -> "restore": {"enabled": true, "every_minutes": 5, "max_age_hours": 6, "settle_seconds": 180, "exclude": ["switch.pompe_arrosage"]}
  enabled        : contrôle automatique (vrai par défaut) ; /restore à la demande marche dans tous les cas ;
  every_minutes  : fréquence du contrôle ; max_age_hours : une prise n'est remise en marche que si elle est revenue il y a moins que ça ;
  settle_seconds : la prise doit être revenue éteinte depuis au moins ce temps (un « off » fugitif au démarrage de HA n'est pas une coupure) ;
  exclude        : entity_id exacts ou préfixes à ne jamais rallumer, en plus de control.exclude (prise du routeur, pompe…).
Domaines : ceux de control.domains (switch par défaut)."""
import json
import os
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

import arm
import control
import summary
from common import CFG, HERE

R = CFG.get("restore", {})
ENABLED = R.get("enabled", True)
EVERY = R.get("every_minutes", 5) * 60
MAX_AGE = R.get("max_age_hours", 6)
SETTLE = R.get("settle_seconds", 180)
EXTRA_EXCLUDE = R.get("exclude", [])
MAX_TRIES = 3  # automatique : au-delà, on abandonne (et on le dit une fois)
STATE_FILE = HERE / "restore_state.json"


def excluded(eid):
    return control.excluded(eid) or any(eid == x or eid.startswith(x) for x in EXTRA_EXCLUDE)


def last_outage(rows):
    """Dernière remise à « off » après une indisponibilité d'une prise qui était « on » : (début de la coupure, retour) ; None s'il n'y en a pas,
    si la prise a été rallumée depuis, ou si elle n'est pas éteinte maintenant."""
    last, gap, event = None, None, None
    for st in rows:
        s = st.get("state")
        if s in ("unavailable", "unknown"):
            gap = gap or summary.ts(st)
        elif s in ("on", "off"):
            if gap is not None and last == "on" and s == "off":
                event = (gap, summary.ts(st))
            elif s == "on":
                event = None
            gap, last = None, s
    return event if rows and rows[-1].get("state") == "off" else None


def candidates(hours):
    """[(entity_id, nom, début de la coupure, retour)] : éteintes maintenant, allumées avant une coupure revenue il y a moins de `hours` h."""
    cur = {s["entity_id"]: s for s in summary.states()}
    ids = [e for e, s in cur.items() if e.split(".")[0] in control.domains() and not summary.is_ghost(s) and s["state"] == "off" and not excluded(e)]
    if not ids:
        return []
    now = datetime.now(timezone.utc)
    hist = summary.history(ids, now - timedelta(hours=hours + 72), now)  # 3 jours de plus : l'état d'avant la coupure
    out = []
    for e in ids:
        ev = last_outage(hist.get(e, []))
        if ev and now - ev[1] <= timedelta(hours=hours) and now - ev[1] >= timedelta(seconds=SETTLE):
            out.append((e, cur[e].get("attributes", {}).get("friendly_name", e), ev[0], ev[1]))
    return sorted(out, key=lambda c: c[1].lower())


def load():
    try:
        d = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save(done):
    cut = (datetime.now() - timedelta(days=30)).isoformat()
    done = {k: v for k, v in done.items() if v.get("t", "") >= cut}  # un mois de mémoire suffit
    try:
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(done, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, STATE_FILE)
    except OSError as e:
        print(f"restore_state.json non écrit : {e}", file=sys.stderr)


def restore(hours, act=True, auto=False):
    """Remet en marche (ou, avec act=False, liste seulement) les prises de `candidates`. -> (lignes, urgent) ; `urgent` : au moins un succès ou un abandon à signaler.
    En mode auto, une prise déjà traitée n'est pas retentée plus de MAX_TRIES fois, et seul un succès ou un abandon fait parler."""
    found = candidates(hours)
    done = load()
    lines, urgent = [], False
    for eid, name, gap, back in found:
        key = f"{eid}|{back:%Y-%m-%dT%H:%M:%S}"
        rec = done.get(key, {})
        when = f"allumée avant la coupure de {gap:%d/%m %Hh%M}, revenue éteinte à {back:%Hh%M}"
        if rec.get("ok"):
            continue
        if not act:
            lines.append(f"• {name} : {when}")
            continue
        if auto and rec.get("tries", 0) >= MAX_TRIES:
            continue
        err = arm.call_service(eid.split(".")[0], "turn_on", [eid])
        time.sleep(3)  # laisser la prise appliquer l'ordre avant de relire son état
        after = next((s["state"] for s in summary.states() if s["entity_id"] == eid), "?")
        ok = err in (None, "(2e essai)") and after == "on"
        done[key] = {"t": datetime.now().isoformat(timespec="seconds"), "tries": rec.get("tries", 0) + 1, "ok": ok}
        if ok:
            lines.append(f"✅ {name} : remise en marche ({when})")
            urgent = True
        elif auto and done[key]["tries"] < MAX_TRIES:
            continue  # on retentera au prochain contrôle, sans bruit
        else:
            lines.append(f"{'❌' if err not in (None, '(2e essai)') else '⚠️'} {name} : {err or 'état relu « ' + after + ' », pas encore allumée'} ({when})")
            urgent = urgent or auto
    if act:
        save(done)
    return lines, urgent


def report(hours=24.0, act=True):
    """/restore [heures] et /restore list [heures] : texte de réponse."""
    lines, _ = restore(hours, act)
    if lines:
        head = "↩️ Remise en marche des prises" if act else "🔎 Prises éteintes après une coupure, qui étaient allumées avant"
        return head + f" (dernières {hours:g} h)\n" + "\n".join(lines)
    return (f"✅ Rien à remettre en marche : aucune prise allumée avant une coupure n'est revenue éteinte ces {hours:g} dernières heures.\n"
            "(Une prise éteinte volontairement, ou exclue par control.exclude / restore.exclude, n'est jamais rallumée.)")


def run():
    time.sleep(120)  # HA et Zigbee finissent de démarrer avec le Pi
    while True:
        try:
            lines, urgent = restore(MAX_AGE, act=True, auto=True)
            if lines and urgent:
                summary.send("↩️ Coupure détectée : prises remises en marche\n" + "\n".join(lines), owner_only=True)
        except Exception as e:  # HA pas encore prêt, réseau : on recommence au prochain contrôle
            print(f"contrôle des prises : {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        time.sleep(EVERY)


def start():
    if ENABLED and EVERY > 0:
        threading.Thread(target=run, daemon=True, name="switch-restore").start()


if __name__ == "__main__":  # python3 switchrestore.py [heures] : liste sans rien actionner
    print(report(float(sys.argv[1]) if sys.argv[1:] else 24.0, act=False))
