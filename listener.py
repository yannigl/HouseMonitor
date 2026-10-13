#!/usr/bin/env python3
"""Écoute les commandes Telegram (long polling sur une connexion persistante) et
pinge Healthchecks toutes les N heures. Lancé par systemd (housemonitor-listener.service).

Commandes (depuis le chat configuré uniquement) :
  /status                      état des services + data du mois
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
from datetime import datetime
from pathlib import Path

from common import CFG, SERVICES, count, heartbeat, load_state, log_net, month_total, save_state

TOKEN = CFG["telegram_token"]
CHAT_ID = str(CFG["telegram_chat_id"])
POLL_TIMEOUT = CFG.get("poll_timeout", 300)        # s, durée max d'un long poll
SLOW_POLL = CFG.get("slow_poll_hours", 4) * 3600    # s, mode éco (/stop listener)
PING_EVERY = CFG.get("ping_interval_hours", 4) * 3600
ME = Path(__file__)


class Telegram:
    """Connexion HTTPS persistante : le handshake TLS n'est payé qu'à la (re)connexion."""

    def __init__(self):
        self.conn = None

    def call(self, method, **params):
        body = urllib.parse.urlencode(params).encode()
        path = f"/bot{TOKEN}/{method}"
        hdrs = {"Content-Type": "application/x-www-form-urlencoded", "Content-Length": str(len(body))}
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

    def close(self):
        if self.conn:
            self.conn.close()
            self.conn = None


def status_text(st):
    icon = lambda b: "🟢" if b else "🔴"
    mode = "long polling" if st["listener"] else f"éco (1 appel / {SLOW_POLL // 3600} h)"
    return (f"{icon(st['listener'])} listener : {mode}\n"
            f"{icon(st['ping'])} ping Healthchecks : {'actif' if st['ping'] else 'arrêté'}"
            f" (toutes les {PING_EVERY // 3600} h)\n"
            f"{icon(st['monitor'])} monitor (résumé du matin) : {'actif' if st['monitor'] else 'arrêté'}\n"
            f"📡 Data 4G ce mois : {month_total() / 1024:.0f} Ko")


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
    return ("Commandes : /status, /stop|/start " + "|".join(SERVICES)
            + "\n\n" + status_text(st))


def main():
    tg = Telegram()
    offset = 0
    last_ping = 0.0
    last_log_day = datetime.now().day
    mtime = ME.stat().st_mtime
    print("listener démarré", flush=True)
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

        try:
            res = tg.call("getUpdates", offset=offset, timeout=POLL_TIMEOUT if st["listener"] else 0,
                          allowed_updates='["message"]')
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
            tg.call("sendMessage", chat_id=CHAT_ID, text=handle(msg["text"], st))

        if not load_state()["listener"]:
            tg.close()  # mode éco : on ferme et on dort, un handshake par réveil
            time.sleep(min(SLOW_POLL, PING_EVERY))


if __name__ == "__main__":
    main()
