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
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from common import CFG, count, data_block, heartbeat, load_state, log_net

HA = CFG.get("ha_url", "http://127.0.0.1:8123")
AUTH = {"Authorization": f"Bearer {CFG['ha_token']}"}
HOURS = CFG.get("hours", 24)
DOMAINS = CFG.get("domains", ["sensor", "binary_sensor", "switch", "light", "input_boolean"])
EXCLUDE = CFG.get("exclude", [])
MAX_TIMES = 5  # au-delà, on n'affiche plus la liste des heures
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


def discover():
    """Entités à résumer : {entity_id: (nom, unité)}."""
    out = {}
    for s in states():
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
    """/update all : état actuel de toutes les entités, groupé par domaine."""
    by_dom = defaultdict(list)
    keep, hidden = snapshot()
    for s in keep:
        eid = s["entity_id"]
        a = s.get("attributes", {})
        val = s["state"] + ("" if s["state"] in ("unknown", "unavailable") else a.get("unit_of_measurement", ""))
        by_dom[eid.split(".")[0]].append(f"• {a.get('friendly_name', eid)} : {val}  ({ts(s):%d/%m %Hh%M})")
    out = [f"🔄 États au {datetime.now():%d/%m %Hh%M}"]
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


def on_events(states):
    """Heures des passages à 'on' (le 1er état peut être un report antérieur)."""
    return [ts(s) for s in states[1:] if s["state"] == "on"]


def alert_line(name, states):
    ev = on_events(states)
    if not ev:
        return None
    times = ", ".join(hm(t) for t in ev) if len(ev) <= MAX_TIMES else f"dernière {hm(ev[-1])}"
    return f"{name} : {len(ev)} alerte{'s' if len(ev) > 1 else ''} ({times})"


def switch_line(name, states):
    ev = on_events(states)
    cur = "allumé" if states and states[-1]["state"] == "on" else "éteint"
    if not ev:
        return None if cur == "éteint" else f"{name} : {cur}, aucun cycle"
    times = ", ".join(hm(t) for t in ev) if len(ev) <= MAX_TIMES else f"dernier {hm(ev[-1])}"
    return f"{name} : {len(ev)} cycle{'s' if len(ev) > 1 else ''} ({times}), {cur}"


def build():
    ents = discover()
    hist = history(list(ents))
    sections = defaultdict(list)
    skipped = []
    for eid, (name, unit) in sorted(ents.items(), key=lambda kv: kv[1][0]):
        states = hist.get(eid, [])
        dom = eid.split(".")[0]
        try:  # une entité aux données inattendues ne doit pas faire échouer tout le résumé
            if dom == "sensor":
                line = numeric_line(name, unit, states)
                key = "📈 Capteurs"
            elif dom == "binary_sensor":
                line = alert_line(name, states)
                key = "🚨 Alertes"
            else:
                line = switch_line(name, states)
                key = "🔌 Switchs / lumières"
        except Exception as e:
            print(f"entité ignorée {eid} : {type(e).__name__}: {e}", file=sys.stderr)
            skipped.append(eid)
            continue
        if line:
            sections[key].append("• " + line)
    text = f"☀️ Résumé du {datetime.now():%d/%m} (dernières {HOURS} h)"
    for key, lines in sections.items():
        text += f"\n\n{key}\n" + "\n".join(lines)
    if skipped:
        shown = ", ".join(skipped[:5]) + ("…" if len(skipped) > 5 else "")
        text += (f"\n\n⚠️ {len(skipped)} entité{'s' if len(skipped) > 1 else ''} ignorée"
                 f"{'s' if len(skipped) > 1 else ''} (données illisibles, détail dans run.log) : {shown}")
    return text


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
