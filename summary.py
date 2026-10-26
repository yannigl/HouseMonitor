#!/usr/bin/env python3
"""Résumé matinal Home Assistant -> Telegram (stdlib uniquement).

Découvre toutes les entités via /api/states, puis résume selon le type :
  sensor numérique  -> min / max (avec l'heure) / valeur actuelle
  binary_sensor     -> nombre d'alertes (passages à "on") + heures
  switch / light /
  input_boolean     -> nombre de cycles (on -> off) + heures de départ
"""
import json
import os
import re
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from common import CFG, count, data_block, fmt_bytes, heartbeat, load_state, log_net, norm

HA = CFG.get("ha_url", "http://127.0.0.1:8123")
AUTH = {"Authorization": f"Bearer {CFG['ha_token']}"}
HOURS = CFG.get("hours", 24)
DOMAINS = CFG.get("domains", ["sensor", "binary_sensor", "switch", "light", "input_boolean"])
EXCLUDE = CFG.get("exclude", [])
MAX_TIMES = 5  # au-delà, on n'affiche plus la liste des heures
VOLT_RANGE = CFG.get("summary", {}).get("voltage_range", [200, 250])  # V : la tension n'est signalée que hors de cette plage
HIDE_NAMES = [norm(x) for x in CFG.get("summary", {}).get("hide", [])]  # lignes absentes du résumé du matin (pas de /update all)
ATTACH = CFG.get("summary", {}).get("attach", {})  # {entity_id: nom d'appareil} : rattache une entité sans appareil à un bloc
RATE_BYTES = {"B/s": 1, "KB/s": 1e3, "kB/s": 1e3, "KiB/s": 1024, "MB/s": 1e6, "MiB/s": 1024 ** 2, "GB/s": 1e9,
              "GiB/s": 1024 ** 3, "bit/s": 1 / 8, "kbit/s": 125, "Mbit/s": 125e3, "Gbit/s": 125e6}  # octets par seconde
DETECTOR_WORDS = ("motion", "mouvement", "pir", "camera", "detecteur", "presence")
UNIT_KWH = {"kWh": 1.0, "Wh": 0.001, "MWh": 1000.0}
HIDE_UNAVAILABLE = CFG.get("hide_unavailable", True)  # /list et /update all : masquer les entités indisponibles
SEND_RETRY_DELAY = 15  # s entre deux essais d'envoi Telegram (DNS / réseau 4G qui flanche)


def http(url, headers=None, data=None):
    req = urllib.request.Request(url, headers=headers or {}, data=data)
    with urllib.request.urlopen(req, timeout=60) as r:
        raw = r.read()
        if not url.startswith(HA):  # trafic internet : on compte
            count(sent=len(data or b"") + len(str(req.header_items())) + 200,
                  recv=len(raw) + len(str(r.headers)), new_connection=True)
        return json.loads(raw)


def ts(state):
    return datetime.fromisoformat(state.get("last_changed") or state["last_updated"]).astimezone()


def hm(dt):
    return f"{dt:%Hh%M}"


def states():
    """Tous les états HA, triés par entity_id."""
    return sorted(http(f"{HA}/api/states", AUTH), key=lambda s: s["entity_id"])


def is_ghost(s):
    """Entité fantôme : connue du registre mais son intégration n'est pas chargée (HA la marque `restored`)."""
    return bool(s.get("attributes", {}).get("restored"))


def snapshot():
    """États à afficher dans /list et /update all, et nombre d'entités masquées.

    Les entités désactivées dans Home Assistant (entité ou intégration) n'existent pas dans son API : elles
    sont déjà absentes. On écarte en plus les fantômes `restored` et, sauf `hide_unavailable: false`,
    les entités actuellement indisponibles."""
    keep, hidden = [], 0
    for s in states():
        if is_ghost(s) or (HIDE_UNAVAILABLE and s["state"] == "unavailable"):
            hidden += 1
        else:
            keep.append(s)
    return keep, hidden


def reported_at(s):
    """Dernière nouvelle de l'entité : `last_reported` (même sans changement de valeur) si HA le fournit."""
    return datetime.fromisoformat(s.get("last_reported") or s.get("last_updated") or s["last_changed"]).astimezone()


def fmt_age(h):
    return f"{h * 60:.0f} min" if h < 1 else f"{h:.0f} h" if h < 48 else f"{h / 24:.0f} j"


def stale_text(hours=24.0):
    """/stale : entités sans nouvelle depuis au moins `hours` h (appareils absents ou à nettoyer), toutes confondues."""
    now = datetime.now().astimezone()
    rows = []
    for s in states():
        age = (now - reported_at(s)).total_seconds() / 3600
        if age >= hours:
            a = s.get("attributes", {})
            rows.append((age, f"• {a.get('friendly_name', s['entity_id'])}  ({s['entity_id']}) : {s['state']}, "
                              f"sans nouvelle depuis {fmt_age(age)}" + (" 👻" if is_ghost(s) else "")))
    if not rows:
        return f"✅ Aucune entité sans nouvelle depuis plus de {hours:g} h."
    out = [f"🕸 {len(rows)} entité{'s' if len(rows) > 1 else ''} sans nouvelle depuis ≥ {hours:g} h"
           " (👻 = fantôme, intégration non chargée)"]
    out += [line for _, line in sorted(rows, reverse=True)]
    out.append("\nPour les retirer : Home Assistant → Settings → Devices & services → désactiver l'intégration "
               "concernée. Un appareil qui n'émet que rarement (porte, bouton) peut figurer ici sans être mort.")
    return "\n".join(out)


def hidden_note(hidden):
    plural = "s" if hidden > 1 else ""
    return (f"\nℹ️ {hidden} entité{plural} masquée{plural} (indisponible{plural} ou fantôme{plural} ; "
            "réglage « hide_unavailable »)") if hidden else ""


def discover(sts=None):
    """Entités à résumer : {entity_id: (nom, unité)}."""
    out = {}
    for s in (states() if sts is None else sts):
        eid = s["entity_id"]
        if is_ghost(s):
            continue
        dom = eid.split(".")[0]
        if dom not in DOMAINS or any(eid.startswith(p) for p in EXCLUDE):
            continue
        attrs = s.get("attributes", {})
        if dom == "sensor" and "unit_of_measurement" not in attrs:
            continue  # on ne garde que les capteurs numériques
        out[eid] = (attrs.get("friendly_name", eid), attrs.get("unit_of_measurement", ""))
    return out


# L'API REST de HA ne connaît pas les appareils (seulement les entités). Le moteur de templates, lui, fait le lien
# entité -> appareil : une seule requête locale renvoie, pour chaque entité, son appareil, fabricant, modèle et pièce.
DEVICE_TEMPLATE = """{% set ns = namespace(rows=[]) %}
{% for s in states %}
{% set d = device_id(s.entity_id) %}
{% set ns.rows = ns.rows + [{'e': s.entity_id, 'd': d, 'n': ((device_attr(d, 'name_by_user') or device_attr(d, 'name')) if d else none), 'm': (device_attr(d, 'manufacturer') if d else none), 'o': (device_attr(d, 'model') if d else none), 'a': area_name(s.entity_id)}] %}
{% endfor %}
{{ ns.rows | tojson }}"""


def devices_map():
    """{entity_id: {"d": id appareil ou None, "n": nom, "m": fabricant, "o": modèle, "a": pièce}}."""
    rows = http(f"{HA}/api/template", {**AUTH, "Content-Type": "application/json"},
                json.dumps({"template": DEVICE_TEMPLATE}).encode())
    return {r["e"]: r for r in rows}


HIDDEN_ATTRS = ("access_token", "entity_picture", "token", "password", "supported_features", "icon")  # secrets et bruit


def state_text(query, limit=12):
    """/state <mot> : état et attributs des entités dont le nom ou l'entity_id contient les mots (équivalent de
    Developer tools → States, pour qui ne l'a pas)."""
    tokens = norm(query).split()
    found = [s for s in states() if all(t in norm(s["entity_id"] + " " + s.get("attributes", {}).get("friendly_name", ""))
                                        for t in tokens)]
    if not found:
        return f"Aucune entité ne correspond à « {query} »."
    out = [f"🔎 {len(found)} entité{'s' if len(found) > 1 else ''} pour « {query} »"
           + (f" (les {limit} premières)" if len(found) > limit else "")]
    for s in sorted(found, key=lambda s: s["entity_id"])[:limit]:
        attrs = {k: v for k, v in s.get("attributes", {}).items() if k not in HIDDEN_ATTRS and k != "friendly_name"}
        out.append(f"\n{s['entity_id']} = {s['state']}  (depuis {ts(s):%d/%m %Hh%M})")
        if s.get("attributes", {}).get("friendly_name"):
            out.append(f"   nom : {s['attributes']['friendly_name']}")
        if attrs:
            out.append("   " + ", ".join(f"{k}={str(v)[:50]}" for k, v in list(attrs.items())[:10]))
    return "\n".join(out)


def list_flat_text():
    """/list all : toutes les entités, groupées par domaine, avec leur entity_id."""
    by_dom = defaultdict(list)
    keep, hidden = snapshot()
    for s in keep:
        eid = s["entity_id"]
        name = s.get("attributes", {}).get("friendly_name", eid)
        by_dom[eid.split(".")[0]].append(f"• {name}  ({eid})")
    out = [f"📋 {sum(len(v) for v in by_dom.values())} entités"]
    for dom, lines in sorted(by_dom.items()):
        out.append(f"\n[{dom}] {len(lines)}\n" + "\n".join(lines))
    return "\n".join(out) + hidden_note(hidden)


def list_text(query=None):
    """/list : les entités regroupées par appareil. /list <mot> : le détail des appareils dont le nom, le fabricant,
    le modèle, la pièce ou une entité contient <mot>."""
    keep, hidden = snapshot()
    try:
        dmap = devices_map()
    except Exception as e:  # HA ancien, template refusé... : on retombe sur la liste à plat
        print(f"regroupement par appareil impossible : {type(e).__name__}: {e}", file=sys.stderr)
        return list_flat_text() + "\n(regroupement par appareil indisponible, voir run.log)"

    devices, loose = {}, []   # appareil -> entités ; entités sans appareil
    for s in keep:
        info = dmap.get(s["entity_id"]) or {}
        if info.get("d"):
            devices.setdefault(info["d"], {"info": info, "ents": []})["ents"].append(s)
        else:
            loose.append(s)
    label = lambda i: i.get("n") or "Appareil sans nom"
    fname = lambda s: s.get("attributes", {}).get("friendly_name", s["entity_id"])
    dom = lambda s: s["entity_id"].split(".")[0]

    if query:
        q = query.lower()
        def match(info, ents):
            hay = " ".join(str(x or "") for x in (info.get("n"), info.get("m"), info.get("o"), info.get("a")))
            return q in hay.lower() or any(q in s["entity_id"].lower() or q in fname(s).lower() for s in ents)
        out = []
        for dev in sorted(devices.values(), key=lambda d: label(d["info"]).lower()):
            if match(dev["info"], dev["ents"]):
                i = dev["info"]
                maker = " · ".join(x for x in (i.get("m"), i.get("o")) if x)
                out.append(f"\n■ {label(i)}" + (f" ({maker})" if maker else "") + (f" — {i['a']}" if i.get("a") else ""))
                out += [f"  • {fname(s)}  ({s['entity_id']})" for s in sorted(dev["ents"], key=lambda s: s["entity_id"])]
        extra = [s for s in loose if q in s["entity_id"].lower() or q in fname(s).lower()]
        if extra:
            out.append("\n■ Sans appareil")
            out += [f"  • {fname(s)}  ({s['entity_id']})" for s in sorted(extra, key=lambda s: s["entity_id"])]
        return (f"🔎 « {query} »" + "".join(f"\n{l}" for l in out) if out else f"Aucun appareil ni entité ne correspond à « {query} ».") \
            + hidden_note(hidden)

    out = [f"📋 {len(devices)} appareil{'s' if len(devices) != 1 else ''} · {len(keep)} entités"
           + (f" (dont {len(loose)} sans appareil)" if loose else "")]
    for dev in sorted(devices.values(), key=lambda d: label(d["info"]).lower()):
        i, ents = dev["info"], dev["ents"]
        counts = ", ".join(f"{n} {d}" for d, n in sorted(Counter(dom(s) for s in ents).items(), key=lambda kv: -kv[1]))
        maker = " · ".join(x for x in (i.get("m"), i.get("o")) if x)
        out.append(f"• {label(i)}" + (f" ({maker})" if maker else "") + f" — {len(ents)} entité{'s' if len(ents) > 1 else ''} : {counts}")
    if loose:
        out.append("\nSans appareil (helpers, automations, soleil…) :")
        for d, n in sorted(Counter(dom(s) for s in loose).items(), key=lambda kv: -kv[1]):
            names = [fname(s) for s in loose if dom(s) == d]
            out.append(f"• {d} ({n}) : " + ", ".join(names[:6]) + ("…" if len(names) > 6 else ""))
    out.append("\n/list <mot> : détail d'un appareil · /list all : toutes les entités")
    return "\n".join(out) + hidden_note(hidden)


def all_states_text():
    """/update all : état actuel de toutes les entités, regroupées par appareil (puis par domaine pour celles sans appareil)."""
    keep, hidden = snapshot()
    try:
        dmap = devices_map()
    except Exception as e:  # sans registre d'appareils : tout est « sans appareil », groupé par domaine
        print(f"regroupement par appareil impossible : {type(e).__name__}: {e}", file=sys.stderr)
        dmap = {}
    devices, loose = {}, []
    for s in keep:
        info = dmap.get(s["entity_id"]) or {}
        if info.get("d"):
            devices.setdefault(info["d"], {"info": info, "ents": []})["ents"].append(s)
        else:
            loose.append(s)

    def value(s):
        a = s.get("attributes", {})
        return s["state"] + ("" if s["state"] in ("unknown", "unavailable") else a.get("unit_of_measurement", ""))

    out = [f"🔄 États au {datetime.now():%d/%m %Hh%M}"]
    for dev in sorted(devices.values(), key=lambda d: (d["info"].get("n") or "").lower()):
        i = dev["info"]
        name = i.get("n") or "Appareil sans nom"
        maker = " · ".join(x for x in (i.get("m"), i.get("o")) if x)
        out.append(f"\n■ {name}" + (f" ({maker})" if maker else "") + (f" — {i['a']}" if i.get("a") else ""))
        for s in sorted(dev["ents"], key=lambda s: s["entity_id"]):
            fn = s.get("attributes", {}).get("friendly_name", s["entity_id"])
            label = fn[len(name):].strip(" -:") or fn if fn.lower().startswith(name.lower()) else fn
            out.append(f"  • {label} : {value(s)}  ({ts(s):%d/%m %Hh%M})")
    by_dom = defaultdict(list)
    for s in loose:
        by_dom[s["entity_id"].split(".")[0]].append(
            f"• {s.get('attributes', {}).get('friendly_name', s['entity_id'])} : {value(s)}  ({ts(s):%d/%m %Hh%M})")
    for dom, lines in sorted(by_dom.items()):
        out.append(f"\n[{dom}]\n" + "\n".join(lines))
    return "\n".join(out) + hidden_note(hidden)


def history(entity_ids):
    start = datetime.now(timezone.utc) - timedelta(hours=HOURS)
    qs = urllib.parse.urlencode(
        {"filter_entity_id": ",".join(entity_ids), "minimal_response": "", "no_attributes": ""}
    )
    data = http(f"{HA}/api/history/period/{start.isoformat()}?{qs}", AUTH)
    return {lst[0]["entity_id"]: lst for lst in data if lst}


def numeric_line(name, unit, states):
    pts = []
    for s in states:
        try:
            pts.append((ts(s), float(s["state"])))
        except (ValueError, KeyError, TypeError):
            pass  # unavailable / unknown / état nul (marque de suppression d'une entité dans l'historique)
    if not pts:
        return None
    lo = min(pts, key=lambda p: p[1])
    hi = max(pts, key=lambda p: p[1])
    if lo[1] == hi[1]:
        return f"{name} : {lo[1]:g}{unit} (stable)"
    return (f"{name} : {lo[1]:g}{unit} à {hm(lo[0])} → {hi[1]:g}{unit} à {hm(hi[0])}, "
            f"actuel {pts[-1][1]:g}{unit}")


def traffic_line(name, unit, states, now=None):
    """Débit (KiB/s…) -> volume écoulé sur la fenêtre (intégrale du débit, approximative) et pic avec son heure."""
    now = now or datetime.now().astimezone()
    start = now - timedelta(hours=HOURS)
    pts = []
    for st in states:
        try:
            pts.append((max(ts(st), start), float(st["state"])))
        except (ValueError, KeyError, TypeError):
            pts.append((max(ts(st), start), None))  # unavailable / unknown : on ne compte rien sur cette période
    if not any(v is not None for _, v in pts):
        return None
    total = 0.0
    for i, (t, v) in enumerate(pts):
        end = pts[i + 1][0] if i + 1 < len(pts) else now
        if v is not None and end > t:
            total += v * (end - t).total_seconds()  # le dernier relevé vaut jusqu'au suivant
    peak_t, peak = max(((t, v) for t, v in pts if v is not None), key=lambda p: p[1])
    low = norm(name)
    sens = "download" if "download" in low else "upload" if "upload" in low else None
    title = {"download": "Downloaded data", "upload": "Uploaded data"}.get(sens, f"{name} (volume)")
    peak_word = {"download": "pic de download", "upload": "pic d'upload"}.get(sens, "pic")
    return (f"{title} : ≈ {fmt_bytes(total * RATE_BYTES[unit])} sur {HOURS} h · {peak_word} {peak:g} {unit} à {hm(peak_t)}")


def on_events(states):
    """Heures des passages à 'on' (le 1er état peut être un report antérieur)."""
    return [ts(s) for s in states[1:] if s["state"] == "on"]


BATTERY_MAX = CFG.get("summary", {}).get("battery_warn", 30)  # % : au-dessus, les batteries ne sont pas affichées


def battery_line(name, unit, states):
    """Batterie : rien si > seuil, sinon un avertissement avec le dernier niveau connu."""
    v = floats(states)
    if not v or v[-1] > BATTERY_MAX:
        return None
    return f"⚠️ {name} : {v[-1]:g}{unit or '%'}"


def is_battery(name):
    return "batter" in norm(name)


def armed_entities(cur):
    """Entités de la config « arm » (groupes et surnoms) : détecteurs et caméras pilotés par /arm."""
    try:
        import arm  # import tardif : arm importe summary
        states = {e: s["state"] for e, s in cur.items()}
        ids = set()
        for g in arm.groups().values():
            ids.update(arm.resolve(g, states)[2])
        for _, ents in arm.nicknames().values():
            ids.update(e for e in ents if e in cur)
        return ids
    except Exception as e:
        print(f"entités armables illisibles : {type(e).__name__}: {e}", file=sys.stderr)
        return set()


def is_detector(eid, name, armed):
    dom = eid.split(".")[0]
    return eid in armed or (dom in ("binary_sensor", "input_boolean", "camera")
                            and any(w in norm(name) for w in DETECTOR_WORDS))


def effective_state(s):
    """État « armé / désarmé » d'une entité armable. Une caméra Netatmo reste « idle » qu'elle surveille ou non : c'est son
    attribut `monitoring` qui dit si elle est armée. -> « on », « off » ou l'état brut (unavailable…)."""
    state = s["state"]
    attrs = s.get("attributes", {})
    if s["entity_id"].startswith("camera.") and "monitoring" in attrs and state not in ("unavailable", "unknown"):
        return "on" if attrs["monitoring"] else "off"
    return state


def armed_line(name, series, current):
    """Détecteur / caméra armable : état actuel et nombre de changements sur la fenêtre. `current` : l'état HA (dict)."""
    camera = bool(current) and current["entity_id"].startswith("camera.") and "monitoring" in current.get("attributes", {})
    state = effective_state(current) if current else (series[-1]["state"] if series else "?")
    if camera:
        series = []  # l'historique ne garde que l'état brut (idle…), pas l'attribut monitoring : pas de changements à compter
    if state in ("unavailable", "unknown"):
        return f"{name} : indisponible"
    status = "désarmé" if state == "off" else "armé"
    ev = [ts(x) for i, x in enumerate(series) if i and x["state"] != series[i - 1]["state"]]
    changes = f"{len(ev)} changement{'s' if len(ev) > 1 else ''} ({times_text(ev, 'dernier')}), " if ev else ""
    return f"{name} : {changes}{status}"


def alert_line(name, states):
    ev = on_events(states)
    if not ev:
        return None
    times = ", ".join(hm(t) for t in ev) if len(ev) <= MAX_TIMES else f"dernière {hm(ev[-1])}"
    return f"{name} : {len(ev)} alerte{'s' if len(ev) > 1 else ''} ({times})"


def switch_info(states, current=None):
    """(heures des passages à 'on', statut actuel en français). `current` : état actuel lu directement dans HA."""
    ev = on_events(states)
    state = current if current is not None else (states[-1]["state"] if states else "off")
    return ev, ("indisponible" if state == "unavailable" else "allumé" if state == "on" else "éteint")


def times_text(ev, last_word):
    return ", ".join(hm(t) for t in ev) if len(ev) <= MAX_TIMES else f"{last_word} {hm(ev[-1])}"


def switch_line(name, states):
    ev, cur = switch_info(states)
    if not ev:
        return None if cur == "éteint" else f"{name} : {cur}, aucun cycle"
    return f"{name} : {len(ev)} cycle{'s' if len(ev) > 1 else ''} ({times_text(ev, 'dernier')}), {cur}"


def floats(states):
    out = []
    for s in states:
        try:
            out.append(float(s["state"]))
        except (ValueError, KeyError, TypeError):
            pass  # unavailable / unknown / état nul
    return out


def consumed_kwh(states, unit):
    """Énergie consommée sur la fenêtre (kWh) d'après un compteur cumulatif ; une baisse = remise à zéro du compteur."""
    f = UNIT_KWH.get(unit)
    v = floats(states)
    if f is None or not v:
        return None
    return sum((b - a) if b >= a else b for a, b in zip(v, v[1:])) * f


def fmt_kwh(x):
    return "0 kWh" if x < 0.005 else f"{x:.2f}".replace(".", ",") + " kWh"


def volt_note(states):
    """Signale la tension seulement si elle sort de la plage normale (les 0 V d'une prise hors mesure sont ignorés)."""
    v = [x for x in floats(states) if x > 0]
    lo, hi = VOLT_RANGE
    if v and (min(v) < lo or max(v) > hi):
        return f"⚠️ tension {min(v):.0f}–{max(v):.0f} V (plage normale {lo:g}–{hi:g} V)"
    return None


def primary_switch(dev_name, switches, fname):
    """L'interrupteur principal d'un appareil : celui qui porte le nom de l'appareil, sinon celui dont l'entity_id
    est le plus court (les autres ajoutent un suffixe : child_lock, backlight, indicator…)."""
    if len(switches) == 1:
        return switches[0]
    dn = norm(dev_name)
    for e in switches:
        if norm(fname[e]) in (dn, dn + " switch", dn + " interrupteur"):
            return e
    return min(switches, key=lambda e: (len(e), e))


def device_lines(ents, hist, dmap, cur):
    """Une ligne par appareil à interrupteur (prises connectées, même inactives) : cycles, statut actuel, énergie totale,
    et la tension seulement si elle est anormale. Puissance, courant et tension normale sont masqués.
    -> (lignes, entités absorbées : l'interrupteur, ses autres interrupteurs et tous ses capteurs)"""
    devs = defaultdict(list)
    for eid in ents:
        d = (dmap.get(eid) or {}).get("d")
        if d:
            devs[d].append(eid)
    lines, absorbed = [], set()
    for members in devs.values():
        switches = sorted(e for e in members if e.startswith("switch."))
        if not switches:
            continue
        fname = {e: ents[e][0] for e in members}
        dev_name = dmap[switches[0]].get("n") or fname[switches[0]]
        sw = primary_switch(dev_name, switches, fname)
        sensors = [e for e in members if e.startswith("sensor.")]
        absorbed.update(switches + sensors)
        ev, status = switch_info(hist.get(sw, []), (cur.get(sw) or {}).get("state"))
        energy = total = None
        meters = [e for e in sensors if ents[e][1] in UNIT_KWH]
        if meters:
            pick = next((e for e in meters if "total" in norm(fname[e])), meters[0])
            energy = consumed_kwh(hist.get(pick, []), ents[pick][1])
            try:  # compteur cumulé actuel, converti en kWh
                total = float((cur.get(pick) or {}).get("state")) * {"Wh": 0.001, "MWh": 1000}.get(ents[pick][1], 1)
            except (TypeError, ValueError):
                pass
        warn = next(filter(None, (volt_note(hist.get(e, [])) for e in sensors if ents[e][1] == "V")), None)
        text = (f"{dev_name} : " + (f"{len(ev)} cycle{'s' if len(ev) > 1 else ''} ({times_text(ev, 'dernier')})"
                                    if ev else "aucun cycle") + f", {status}")
        if total is not None:
            text += f" · énergie totale {fmt_kwh(total)}"
            if energy is not None:
                text += f" (+{fmt_kwh(energy)} sur {HOURS} h)"
        elif energy is not None:
            text += f" · énergie {HOURS} h : {fmt_kwh(energy)}"
        if warn:
            text += f" · {warn}"
        lines.append(text)
    return sorted(lines, key=str.lower), absorbed


UNDERVOLT = re.compile(r"under-?voltage detected", re.I)


def undervoltage_events(hours=24):
    """Heures (epoch) des « Under-voltage detected » du noyau sur la fenêtre, ou None si le journal n'est pas lisible.
    Le journal de ce Pi est volatil : il ne remonte pas avant le dernier démarrage."""
    r = subprocess.run(["journalctl", "-k", "-o", "short-unix", "--no-pager", "-q", "--since", f"{hours} hours ago"],
                       capture_output=True, text=True, timeout=20)
    if r.returncode != 0:
        return None
    out = []
    for line in r.stdout.splitlines():
        if UNDERVOLT.search(line):
            try:
                out.append(float(line.split()[0]))
            except (ValueError, IndexError):
                pass
    return out


def power_text():
    """Ligne « alimentation du Pi » : nombre d'alertes de sous-tension sur la fenêtre. Vide si rien n'est lisible
    (autre machine que le Pi, journal inaccessible)."""
    try:
        events = undervoltage_events(HOURS)
    except Exception as e:
        print(f"journal du noyau illisible : {type(e).__name__}: {e}", file=sys.stderr)
        return ""
    if events is None:
        return ""
    now_flag = ""
    try:  # état instantané : bit 0 = sous-tension en ce moment, bit 2 = CPU bridé
        v = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True, text=True, timeout=10).stdout
        flags = int(v.split("=")[1], 16)
        now_flag = (" · ⚠️ sous-tension en ce moment" if flags & 0x1 else "") + (" · CPU bridé" if flags & 0x4 else "")
    except Exception:
        pass
    try:  # fenêtre réellement couverte : le journal est volatil, il repart à zéro à chaque démarrage
        uptime_h = float(open("/proc/uptime").read().split()[0]) / 3600
    except (OSError, ValueError):
        uptime_h = HOURS
    since = (f" (depuis le démarrage, il y a {uptime_h:.0f} h)" if uptime_h < HOURS else f" sur {HOURS} h")
    if not events:
        return f"\n\n⚡ Alimentation du Pi : aucune alerte de sous-tension{since}{now_flag}"
    last = datetime.fromtimestamp(events[-1])
    return (f"\n\n⚡ Alimentation du Pi : {len(events)} alerte{'s' if len(events) > 1 else ''} de sous-tension{since}, "
            f"dernière à {last:%Hh%M}{now_flag}\n   → alimentation ou câble à vérifier")


def build():
    sts = states()
    cur = {s["entity_id"]: s for s in sts}
    ents = discover(sts)
    armed = armed_entities(cur)
    for eid in armed:  # caméras et autres entités armables : résumées même si leur domaine n'est pas dans « domains »
        if eid in cur and eid not in ents and not is_ghost(cur[eid]):
            ents[eid] = (cur[eid].get("attributes", {}).get("friendly_name", eid), "")
    hist = history(list(ents))
    sections = defaultdict(list)
    skipped = []
    dev_lines, absorbed, dmap = [], set(), {}
    try:  # regroupement par appareil : s'il échoue, on retombe sur une ligne par entité
        dmap = devices_map()
        dev_lines, absorbed = device_lines(ents, hist, dmap, cur)
    except Exception as e:
        print(f"regroupement par appareil impossible : {type(e).__name__}: {e}", file=sys.stderr)
    # appareils ayant au moins un détecteur / une caméra ; appareils ayant un aspirateur (Roomba…)
    det_devs = {dmap[e]["d"] for e, (n, _) in ents.items() if (dmap.get(e) or {}).get("d") and is_detector(e, n, armed)}
    vac_devs = {r["d"] for e, r in dmap.items() if e.startswith("vacuum.") and r.get("d")}
    groups = defaultdict(list)  # appareil -> ses lignes
    det_groups = defaultdict(list)  # appareil de détection -> ses lignes
    DET = "🛡 Détecteurs / caméras"
    for eid, (name, unit) in sorted(ents.items(), key=lambda kv: kv[1][0]):
        if eid in absorbed or any(h in norm(name) for h in HIDE_NAMES):
            continue
        series = hist.get(eid, [])
        dom = eid.split(".")[0]
        info = dmap.get(eid) or {}
        dev = info.get("d")
        dname = info.get("n") if dev else ATTACH.get(eid)
        label = name  # sous un appareil, son nom est retiré du libellé de l'entité
        if dname and name.lower().startswith(dname.lower()):
            label = name[len(dname):].strip(" -:") or name
        detector = is_detector(eid, name, armed) or dev in det_devs
        try:  # une entité aux données inattendues ne doit pas faire échouer tout le résumé
            if dom == "sensor":
                if is_battery(name):
                    line = battery_line(label, unit, series)  # rien au-dessus du seuil, ⚠️ en dessous
                elif unit in RATE_BYTES:
                    line = traffic_line(label, unit, series)  # volume (intégrale du débit) et pic, plutôt que la vitesse
                else:
                    line = numeric_line(label, unit, series)
                    if line and dev in vac_devs and line.endswith("(stable)"):
                        # aspirateur : seulement le total des missions et ce qui a varié sur la fenêtre
                        line = line.replace("(stable)", "(-)") if "total missions" in norm(name) else None
                key = "📈 Capteurs"
            elif dom == "binary_sensor":
                line = alert_line(label, series)
                key = "🚨 Alertes"
            elif eid in armed or dom == "camera":
                line = armed_line(label, series, cur.get(eid))
                key = DET
            else:
                line = switch_line(label, series)
                key = "🔌 Switchs / lumières"
            if detector and dom in ("binary_sensor", "input_boolean", "camera"):
                key = DET
            if dom == "input_boolean" and detector:
                line = armed_line(label, series, cur.get(eid))
        except Exception as e:
            print(f"entité ignorée {eid} : {type(e).__name__}: {e}", file=sys.stderr)
            skipped.append(eid)
            continue
        if line:
            if dname and (dev in det_devs or (not dev and detector)):
                det_groups[dname].append("  • " + line)
            elif dname:
                groups[dname].append("  • " + line)
            else:
                sections[key].append("• " + line)
    sections["🔌 Switchs / lumières"] += ["• " + l for l in dev_lines]
    text = f"☀️ Résumé du {datetime.now():%d/%m} (dernières {HOURS} h)"
    for key in ("📈 Capteurs", "🚨 Alertes", "🔌 Switchs / lumières", DET):  # ordre fixe
        body = sorted(sections.get(key, []), key=str.lower)
        if key == DET:
            for dname in sorted(det_groups, key=str.lower):
                body += [f"■ {dname}"] + sorted(det_groups[dname], key=str.lower)
        if body:
            text += f"\n\n{key}\n" + "\n".join(body)
    if groups:
        text += "\n\n🏠 Appareils"
        for dname in sorted(groups, key=str.lower):
            text += f"\n■ {dname}\n" + "\n".join(sorted(groups[dname], key=str.lower))
    if skipped:
        shown = ", ".join(skipped[:5]) + ("…" if len(skipped) > 5 else "")
        text += (f"\n\n⚠️ {len(skipped)} entité{'s' if len(skipped) > 1 else ''} ignorée"
                 f"{'s' if len(skipped) > 1 else ''} (données illisibles, détail dans run.log) : {shown}")
    return text + power_text()


def send(text):
    # Telegram limite à 4096 caractères par message
    for i in range(0, len(text), 4000):
        for attempt in (1, 2, 3):
            try:
                http(
                    f"https://api.telegram.org/bot{CFG['telegram_token']}/sendMessage",
                    data=urllib.parse.urlencode(
                        {"chat_id": CFG["telegram_chat_id"], "text": text[i:i + 4000]}
                    ).encode(),
                )
                break
            except urllib.error.HTTPError as e:  # 4xx = notre faute, inutile de réessayer
                if e.code < 500 or attempt == 3:
                    raise
            except OSError:  # DNS, connexion, timeout
                if attempt == 3:
                    raise
            time.sleep(SEND_RETRY_DELAY)


def net_line():
    return "\n\n" + data_block()


def main():
    try:
        git_bytes = int(os.environ.get("GIT_BYTES") or 0)  # fourni par run.sh
    except ValueError:
        git_bytes = 0
    if "--dry" in sys.argv:
        print(build() + net_line())
        return
    if not load_state()["monitor"]:
        heartbeat()  # le script a bien tourné, il est juste en pause (/stop monitor)
        log_net("summary", git_bytes)
        return
    try:
        try:
            footer = net_line()
        except Exception:  # le compteur de data ne doit jamais empêcher le résumé
            footer = ""
            traceback.print_exc()
        send(build() + footer)
    except Exception:
        err = traceback.format_exc()
        print(err, file=sys.stderr)
        heartbeat(ok=False, body=err)  # la trace est lisible dans le Log du check
        try:
            log_net("summary", git_bytes)  # la data d'un run en échec compte aussi (pull, Telegram partiel)
        except Exception:
            traceback.print_exc()
        sys.exit(3)  # 3 = échec déjà signalé à Healthchecks (run.sh ne le redouble pas)
    heartbeat()
    try:
        log_net("summary", git_bytes)
    except Exception:  # la comptabilité ne doit pas transformer un envoi réussi en alerte
        traceback.print_exc()


if __name__ == "__main__":
    main()
