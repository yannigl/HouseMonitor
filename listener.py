#!/usr/bin/env python3
"""Écoute les commandes Telegram (long polling sur une connexion persistante) et
pinge Healthchecks toutes les N heures. Lancé par systemd (housemonitor-listener.service).

Commandes (depuis le chat configuré uniquement) :
  /status                      état des services + data 4G
  /data                        data 4G utilisée depuis le dernier reset
  /data reset                  remet le compteur à 0 (puis reset auto le même jour du mois suivant)
  /list                        liste des appareils HA (nom + entity_id) par domaine
  /update                      le résumé du matin, maintenant
  /update all                  état actuel de toutes les entités (dernier changement)
  /stale [heures]              entités sans nouvelle depuis N h (24 par défaut) : les appareils à nettoyer
  Les réponses > 4000 caractères partent en pièce jointe .txt.
  /stop ping   | /start ping   heartbeat Healthchecks
  /stop monitor| /start monitor résumé du matin
  /shutdown                    éteint le Pi après confirmation (/shutdown confirm), alias : /stop pi
  /stop listener               passe en mode éco : 1 appel toutes les `slow_poll_hours` h
  /start listener              retour au long polling (réactif)
"""
import http.client
import json
import os
import signal
import subprocess
import sys
import time
import urllib.parse
import uuid
from datetime import datetime
from pathlib import Path

import summary
from common import (CFG, SERVICES, count, data_block, data_compact, heartbeat, load_state, log_net,
                    pi_cum, reset_cycle, save_state)

TOKEN = CFG["telegram_token"]
CHAT_ID = str(CFG["telegram_chat_id"])
TG = CFG.get("listener", {})
HC = CFG.get("healthchecks", {})
POLL_TIMEOUT = TG.get("poll_timeout", 300)          # s, durée max d'un long poll
SLOW_POLL = TG.get("slow_poll_hours", 24) * 3600    # s, mode éco (/stop listener)
PING_EVERY = HC.get("ping_interval_hours", 4) * 3600
ALERT_AFTER = HC.get("period_hours", 4) + HC.get("grace_hours", 1)
SHUTDOWN_SIGNAL = CFG.get("shutdown", {}).get("healthchecks", "fail")  # "fail" | "log" | "none"
CONFIRM_WINDOW = 60   # s pour confirmer /shutdown
SHUTDOWN_DELAY = 5    # s entre l'acquittement et l'extinction
PENDING = {"t": None}  # demande d'extinction en attente (en mémoire : perdue au reboot, voulu)
ME = Path(__file__)


class Telegram:
    """Connexion HTTPS persistante : le handshake TLS n'est payé qu'à la (re)connexion."""

    def __init__(self):
        self.conn = None

    def call(self, method, _body=None, _ctype=None, **params):
        body = _body if _body is not None else urllib.parse.urlencode(params).encode()
        path = f"/bot{TOKEN}/{method}"
        hdrs = {"Content-Type": _ctype or "application/x-www-form-urlencoded",
                "Content-Length": str(len(body))}
        for attempt in (1, 2):
            new = self.conn is None
            try:
                if new:
                    self.conn = http.client.HTTPSConnection("api.telegram.org", timeout=POLL_TIMEOUT + 30)
                self.conn.request("POST", path, body, hdrs)
                r = self.conn.getresponse()
                raw = r.read()
                count(sent=len(path) + len(body) + 120, recv=len(raw) + len(str(r.headers)), new_connection=new)
                return json.loads(raw)
            except (http.client.HTTPException, OSError) as e:
                self.close()
                if attempt == 2:
                    raise
                print(f"telegram : reconnexion ({e})", file=sys.stderr)

    def reply(self, text, filename="housemonitor.txt"):
        """Message court, ou pièce jointe .txt au-delà de la limite Telegram (4096)."""
        if len(text) <= 4000:
            return self.call("sendMessage", chat_id=CHAT_ID, text=text)
        b = f"----{uuid.uuid4().hex}"
        part = lambda name, val, extra="": (f"--{b}\r\nContent-Disposition: form-data; name=\"{name}\"{extra}"
                                            f"\r\n\r\n").encode() + val + b"\r\n"
        body = (part("chat_id", CHAT_ID.encode())
                + part("caption", text.splitlines()[0].encode())
                + part("document", text.encode(), f'; filename="{filename}"\r\nContent-Type: text/plain')
                + f"--{b}--\r\n".encode())
        return self.call("sendDocument", _body=body, _ctype=f"multipart/form-data; boundary={b}")

    def close(self):
        if self.conn:
            self.conn.close()
            self.conn = None


COMMANDS = [
    ("status", "État des services + data 4G"),
    ("data", "Data 4G : script et Pi — /data reset : remise à zéro"),
    ("list", "Appareils HA par domaine (nom + entity_id)"),
    ("update", "Le résumé du matin, maintenant — /update all : état de tout"),
    ("stale", "Entités sans nouvelle depuis N h (24 par défaut) : appareils à nettoyer"),
    ("shutdown", "Éteindre le Pi (avec confirmation)"),
    ("stop", "stop listener|ping|monitor"),
    ("start", "start listener|ping|monitor"),
    ("help", "Cette aide"),
]

HELP = "🛠 Commandes\n" + "\n".join(f"/{c} — {d}" for c, d in COMMANDS) + (
    "\n\nlistener : écoute Telegram (stop = mode éco, 1 appel / jour)"
    "\nping : heartbeat Healthchecks (stop = alerte après la grâce)"
    "\nmonitor : résumé du matin (stop = le cron tourne mais n'envoie rien)")


def can_poweroff():
    """Le droit d'éteindre est-il en place (règle sudoers de setup-power.sh) ?"""
    if os.geteuid() == 0:
        return True
    try:
        return subprocess.run(["sudo", "-n", "-l", "systemctl", "poweroff"], capture_output=True,
                              timeout=10).returncode == 0
    except Exception:
        return False


def shutdown_signal_text(st):
    if SHUTDOWN_SIGNAL == "none" or not st["ping"]:
        return "aucun" + (" (ping désactivé)" if not st["ping"] else "")
    return {"fail": "/fail (alerte DOWN immédiate, puis UP au redémarrage)",
            "log": "/log (note dans le journal, sans alerte)"}.get(SHUTDOWN_SIGNAL, "aucun")


def power_off(tg, offset):
    """Exécuté APRÈS l'envoi de l'acquittement."""
    st = load_state()
    # 1. Acquitter la commande chez Telegram : sans cela elle serait relivrée au prochain démarrage.
    try:
        tg.call("getUpdates", offset=offset, timeout=0)
    except Exception as e:
        print(f"acquittement Telegram KO : {e}", file=sys.stderr)
    # 2. Signal à Healthchecks
    signalled = False
    if SHUTDOWN_SIGNAL == "fail":
        signalled = heartbeat(ok=False, body="arrêt volontaire demandé par /shutdown")
    elif SHUTDOWN_SIGNAL == "log":
        heartbeat(endpoint="/log", body="arrêt volontaire demandé par /shutdown")
    # 3. Enregistrer la data en cours (les compteurs repartent à 0 au prochain démarrage)
    try:
        log_net("listener")
        pi_cum()
    except Exception as e:
        print(f"enregistrement data KO : {e}", file=sys.stderr)
    time.sleep(SHUTDOWN_DELAY)
    cmd = ["systemctl", "poweroff"] if os.geteuid() == 0 else ["sudo", "-n", "systemctl", "poweroff"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:  # le Pi tourne toujours : on annule l'échec signalé
        if signalled:
            heartbeat()
        tg.reply(f"❌ Extinction impossible : {(r.stderr or r.stdout).strip()[:300]}")


def status_text(st):
    icon = lambda b: "🟢" if b else "🔴"
    mode = "long polling" if st["listener"] else f"éco (1 appel / {SLOW_POLL // 3600} h)"
    return (f"{icon(st['listener'])} listener : {mode}\n"
            f"{icon(st['ping'])} ping Healthchecks : {'actif' if st['ping'] else 'arrêté'}"
            f" (toutes les {PING_EVERY // 3600} h, alerte après {ALERT_AFTER} h sans ping)\n"
            f"{icon(st['monitor'])} monitor (résumé du matin) : {'actif' if st['monitor'] else 'arrêté'}\n"
            f"{data_compact()}")


def handle(text, st):
    parts = text.lower().split()
    cmd = parts[0].split("@")[0] if parts else ""
    args = parts[1:]
    if cmd == "/stop" and args[:1] == ["pi"]:
        cmd, args = "/shutdown", args[1:]
    if cmd == "/shutdown":
        if args[:1] != ["confirm"]:
            if not can_poweroff():
                return "❌ Extinction non autorisée : lancez d'abord sudo ./setup-power.sh sur le Pi."
            PENDING["t"] = time.monotonic()
            return (f"⚠️ Éteindre le Pi ?\n• signal Healthchecks : {shutdown_signal_text(st)}\n"
                    "• il ne redémarrera pas seul : il faudra remettre le courant (ou le débrancher/rebrancher)\n\n"
                    f"Confirmez dans les {CONFIRM_WINDOW} s : /shutdown confirm")
        t = PENDING["t"]
        PENDING["t"] = None
        if t is None or time.monotonic() - t > CONFIRM_WINDOW:
            return "Aucune demande d'extinction en attente (ou expirée). Refaites /shutdown."
        return (f"⏻ Extinction dans {SHUTDOWN_DELAY} s.\nSignal Healthchecks : {shutdown_signal_text(st)}.\n"
                "Le Pi redémarrera quand le courant sera remis.", power_off)
    if cmd in ("/stop", "/start") and len(parts) > 1 and parts[1] in SERVICES:
        st[parts[1]] = cmd == "/start"
        save_state(st)
        note = ""
        if parts[1] == "ping" and not st["ping"]:
            note = "\n⚠️ Healthchecks va alerter après la période de grâce."
        return f"✅ {parts[1]} → {'start' if st[parts[1]] else 'stop'}{note}\n\n" + status_text(st)
    if cmd == "/status":
        return status_text(st)
    if cmd == "/help":
        return HELP
    if cmd == "/data":
        if parts[1:2] == ["reset"]:
            before = data_compact()
            log_net("listener")  # les octets en cours restent dans l'ancien cycle
            reset_cycle()
            return f"✅ Compteur remis à zéro\nAvant : {before}\n\n{data_block()}"
        return data_block()
    try:
        if cmd == "/stale":
            try:
                hours = float(parts[1]) if parts[1:2] else 24.0
            except ValueError:
                return "Usage : /stale [heures]   (24 par défaut)"
            return summary.stale_text(hours)
        if cmd == "/list":
            return summary.list_text()
        if cmd == "/update":
            return summary.all_states_text() if parts[1:2] == ["all"] else summary.build()
    except Exception as e:
        return f"❌ Home Assistant : {e}"
    return f"❓ Commande inconnue : {text.split()[0] if parts else ''}\n\n" + HELP


def loop():
    tg = Telegram()
    offset = 0
    last_ping = last_poll = last_sample = 0.0
    last_log_day = datetime.now().day
    mtime = ME.stat().st_mtime
    print("listener démarré", flush=True)
    try:  # menu "/" de Telegram ; ~0,6 Ko, une fois au démarrage
        tg.call("setMyCommands", commands=json.dumps([{"command": c, "description": d} for c, d in COMMANDS]))
    except Exception as e:
        print(f"setMyCommands KO : {e}", file=sys.stderr)
    while True:
        st = load_state()
        now = time.time()

        if now - last_ping >= PING_EVERY:
            heartbeat()  # ne fait rien si /stop ping
            last_ping = now

        if now - last_sample >= 3600:  # compteur du Pi : une lecture par heure (il repart à 0 au reboot)
            pi_cum()
            last_sample = now

        if datetime.now().day != last_log_day:  # une ligne de data par jour
            log_net("listener")
            last_log_day = datetime.now().day

        if ME.stat().st_mtime != mtime:  # mis à jour par le git pull du matin
            print("code mis à jour, redémarrage", flush=True)
            log_net("listener")
            pi_cum()
            os.execv(sys.executable, [sys.executable, str(ME)])

        if not st["listener"] and now - last_poll < SLOW_POLL:
            # mode éco : on dort jusqu'au prochain ping ou au prochain poll, sans toucher au réseau
            tg.close()
            time.sleep(max(60, min(last_ping + PING_EVERY, last_poll + SLOW_POLL) - now))
            continue

        try:
            res = tg.call("getUpdates", offset=offset, timeout=POLL_TIMEOUT if st["listener"] else 0,
                          allowed_updates='["message"]')
            last_poll = time.time()
        except Exception as e:
            print(f"getUpdates KO : {e}", file=sys.stderr)
            time.sleep(60)
            continue

        for upd in res.get("result", []):
            offset = upd["update_id"] + 1
            msg = upd.get("message", {})
            if str(msg.get("chat", {}).get("id")) != CHAT_ID or "text" not in msg:
                continue  # on n'obéit qu'à notre chat
            st = load_state()
            out = handle(msg["text"], st)
            text, after = out if isinstance(out, tuple) else (out, None)
            tg.reply(text)
            if after:
                after(tg, offset)


def main():
    # systemd envoie SIGTERM à l'arrêt et au reboot : on enregistre alors la data en cours avant de partir
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        loop()
    finally:
        try:
            log_net("listener")
            pi_cum()
        except Exception as e:
            print(f"enregistrement final KO : {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
