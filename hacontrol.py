"""Home Assistant à distance, par son API REST (même jeton que le reste) :
/restart netatmo        recharge l'intégration Netatmo (le « Recharger » de Settings → Devices & services) : HA ne redémarre pas
/restart homeassistant  redémarre HA après vérification de sa configuration ; attend son retour
Le jeton doit être celui d'un administrateur de HA (l'API des intégrations lui est réservée)."""
import json
import time
import urllib.error
import urllib.request

import summary

HEAD = {**summary.AUTH, "Content-Type": "application/json"}
STATES = {"loaded": "chargée", "setup_retry": "nouvelle tentative en cours (service injoignable ?)", "setup_error": "échec du démarrage",
          "not_loaded": "non chargée", "migration_error": "échec de migration", "failed_unload": "échec de l'arrêt"}


def call(method, path, body=None, timeout=60):
    req = urllib.request.Request(f"{summary.HA}{path}", method=method, headers=HEAD,
                                 data=json.dumps(body).encode() if body is not None else None)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    return json.loads(raw) if raw.strip() else None


def http_error(e):
    try:
        detail = json.loads(e.read()).get("message") or ""
    except Exception:
        detail = ""
    return f"HTTP {e.code}" + (f" : {detail[:300]}" if detail else "")


def entries(domain):
    return call("GET", f"/api/config/config_entries/entry?domain={domain}") or []


def reload_integration(domain="netatmo"):
    """Recharge chaque entrée de l'intégration. -> (tout va bien, texte)."""
    try:
        found = entries(domain)
    except urllib.error.HTTPError as e:
        return False, f"HA refuse la liste des intégrations ({http_error(e)}). Le jeton de config.json doit être celui d'un administrateur."
    if not found:
        return False, f"Aucune intégration « {domain} » dans HA."
    lines, ok_all = [], True
    for en in found:
        title = en.get("title") or domain
        try:
            call("POST", f"/api/config/config_entries/entry/{en['entry_id']}/reload", timeout=150)
        except urllib.error.HTTPError as e:
            ok_all = False
            lines.append(f"❌ {title} : rechargement refusé ({http_error(e)})")
            continue
        except OSError as e:
            ok_all = False
            lines.append(f"❌ {title} : pas de réponse de HA ({type(e).__name__})")
            continue
        now = next((x for x in entries(domain) if x.get("entry_id") == en["entry_id"]), en)
        state = now.get("state")
        good = state == "loaded"
        ok_all &= good
        lines.append(f"{'✅' if good else '⚠️'} {title} : {STATES.get(state, state)}" + (f"\n   {now['reason']}" if now.get("reason") and not good else ""))
    if ok_all:
        lines.append("Les capteurs reviennent dans la minute. Le prochain résumé les reprendra.")
    return ok_all, "\n".join(lines)


def restart_ha(wait=420):
    """Demande le redémarrage, puis attend que l'API s'arrête et revienne. -> (ok, texte)."""
    t0 = time.time()
    try:
        call("POST", "/api/services/homeassistant/restart", {}, timeout=30)
    except urllib.error.HTTPError as e:  # configuration invalide : HA refuse de redémarrer
        return False, f"❌ HA a refusé le redémarrage ({http_error(e)}). Il tourne toujours."
    except (OSError, ValueError):  # HA coupe la connexion en s'arrêtant : normal
        pass
    went_down = False
    while time.time() - t0 < wait:
        time.sleep(5)
        try:
            call("GET", "/api/", timeout=8)
            up = True
        except urllib.error.HTTPError:
            up = False  # 502/503 : en cours de démarrage
        except (OSError, ValueError):
            up = False
        if not up:
            went_down = True
        elif went_down:
            return True, f"✅ Home Assistant est de retour ({time.time() - t0:.0f} s). Ses intégrations (Netatmo, Zigbee…) mettent encore 1 à 2 minutes à se charger."
        elif time.time() - t0 > 120:
            break
    if not went_down:
        return False, "⚠️ Home Assistant n'a pas semblé s'arrêter : le redémarrage n'a peut-être pas eu lieu. Vérifiez dans HA."
    return False, f"⚠️ Home Assistant n'est pas revenu après {wait // 60} minutes : vérifiez sur place ou dans le journal du Pi (docker logs homeassistant)."
