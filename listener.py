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
  Les réponses > 4000 caractères partent en pièce jointe .txt.
  /stop ping   | /start ping   heartbeat Healthchecks
  /stop monitor| /start monitor résumé du matin
  /stop listener               passe en mode éco : 1 appel toutes les `slow_poll_hours` h
  /start listener              retour au long polling (réactif)
"""
import http.client
import json
import os
import sys
import time
import urllib.parse
import uuid
from datetime import datetime
from pathlib import Path

import summary
from common import (CFG, SERVICES, count, cycle_line, heartbeat, load_state, log_net,
                    reset_cycle, save_state)

TOKEN = CFG["telegram_token"]
CHAT_ID = str(CFG["telegram_chat_id"])
TG = CFG.get("listener", {})
HC = CFG.get("healthchecks", {})
POLL_TIMEOUT = TG.get("poll_timeout", 300)          # s, durée max d'un long poll
SLOW_POLL = TG.get("slow_poll_hours", 24) * 3600    # s, mode éco (/stop listener)
PING_EVERY = HC.get("ping_interval_hours", 4) * 3600
ALERT_AFTER = HC.get("period_hours", 4) + HC.get("grace_hours", 1)
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
    ("data", "Data 4G utilisée — /data reset : remise à zéro"),
    ("list", "Appareils HA par domaine (nom + entity_id)"),
    ("update", "Le résumé du matin, maintenant — /update all : état de tout"),
    ("stop", "stop listener|ping|monitor"),
    ("start", "start listener|ping|monitor"),
    ("help", "Cette aide"),
]

HELP = "🛠 Commandes\n" + "\n".join(f"/{c} — {d}" for c, d in COMMANDS) + (
    "\n\nlistener : écoute Telegram (stop = mode éco, 1 appel / jour)"
    "\nping : heartbeat Healthchecks (stop = alerte après la grâce)"
    "\nmonitor : résumé du matin (stop = le cron tourne mais n'envoie rien)")


def status_text(st):
    icon = lambda b: "🟢" if b else "🔴"
    mode = "long polling" if st["listener"] else f"éco (1 appel / {SLOW_POLL // 3600} h)"
    return (f"{icon(st['listener'])} listener : {mode}\n"
            f"{icon(st['ping'])} ping Healthchecks : {'actif' if st['ping'] else 'arrêté'}"
            f" (toutes les {PING_EVERY // 3600} h, alerte après {ALERT_AFTER} h sans ping)\n"
            f"{icon(st['monitor'])} monitor (résumé du matin) : {'actif' if st['monitor'] else 'arrêté'}\n"
            f"📡 Data 4G : {cycle_line()}")


def handle(text, st):
    parts = text.lower().split()
    cmd = parts[0].split("@")[0] if parts else ""
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
            before = cycle_line()
            log_net("listener")  # les octets en cours restent dans l'ancien cycle
            reset_cycle()
            return f"✅ Compteur remis à zéro (était : {before})\n\n📡 Data 4G : {cycle_line()}"
        return f"📡 Data 4G : {cycle_line()}"
    try:
        if cmd == "/list":
            return summary.list_text()
        if cmd == "/update":
            return summary.all_states_text() if parts[1:2] == ["all"] else summary.build()
    except Exception as e:
        return f"❌ Home Assistant : {e}"
    return f"❓ Commande inconnue : {text.split()[0] if parts else ''}\n\n" + HELP


def main():
    tg = Telegram()
    offset = 0
    last_ping = last_poll = 0.0
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

        if datetime.now().day != last_log_day:  # une ligne de data par jour
            log_net("listener")
            last_log_day = datetime.now().day

        if ME.stat().st_mtime != mtime:  # mis à jour par le git pull du matin
            print("code mis à jour, redémarrage", flush=True)
            log_net("listener")
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
            tg.reply(handle(msg["text"], st))


if __name__ == "__main__":
    main()
