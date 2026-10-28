#!/usr/bin/env python3
"""Écoute les commandes Telegram (long polling sur une connexion persistante) et
pinge Healthchecks toutes les N heures. Lancé par systemd (housemonitor-listener.service).

Commandes (depuis le chat configuré uniquement) :
  /status                      état des services + data 4G
  /data                        data 4G utilisée depuis le dernier reset
  /data reset                  remet le compteur à 0 (puis reset auto le même jour du mois suivant)
  /list                        les appareils HA, avec le nombre et le type de leurs entités
  /list <mot>                  le détail des appareils (ou entités) dont le nom, la pièce… contient <mot>
  /list all                    toutes les entités à plat, par domaine (nom + entity_id)
  /update                      le résumé du matin, maintenant
  /update all                  état actuel de toutes les entités (dernier changement)
  /on <nom> | /off <nom>               allume / éteint l'interrupteur d'un appareil (nom approximatif accepté)
  /arm all | <groupe> | <appareil>     arme (turn_on) les groupes définis dans config.json, ou un appareil d'un groupe
  /disarm all | <groupe> | <appareil>  désarme (turn_off), mêmes cibles (alias : /unarm)
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

import alerts
import arm
import smsfwd
import control
import summary
import vacuum
from common import (CFG, HERE, SERVICES, count, data_block, data_compact, heartbeat, load_state, log_net,
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
SHUTDOWN_DRY_RUN = CFG.get("shutdown", {}).get("dry_run", False)  # True : tout sauf l'extinction
CONFIRM_WINDOW = 60   # s pour confirmer /shutdown
SHUTDOWN_DELAY = 5    # s entre l'acquittement et l'extinction
PING_RETRY = 300  # s avant de retenter un ping Healthchecks qui a échoué (réseau coupé un instant)
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

    def reply(self, text, filename="housemonitor.txt", chat_id=None):
        """Message court, ou pièce jointe .txt au-delà de la limite Telegram (4096). Par défaut dans le chat du propriétaire."""
        chat_id = str(chat_id if chat_id is not None else CHAT_ID)
        if len(text) <= 4000:
            return self.call("sendMessage", chat_id=chat_id, text=text)
        b = f"----{uuid.uuid4().hex}"
        part = lambda name, val, extra="": (f"--{b}\r\nContent-Disposition: form-data; name=\"{name}\"{extra}"
                                            f"\r\n\r\n").encode() + val + b"\r\n"
        body = (part("chat_id", chat_id.encode())
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
    ("list", "Appareils HA regroupés — /list <mot> : détail, /list all : toutes les entités"),
    ("update", "Le résumé du matin, maintenant — /update all : tout, /update <mot> : ce qui contient le mot"),
    ("on", "Allumer un interrupteur : /on <nom de l'appareil>"),
    ("off", "Éteindre un interrupteur : /off <nom de l'appareil>"),
    ("launch", "Aspirateur : lancer l'aspiration standard — /launch <nom>"),
    ("send", "Aspirateur : retour à la base — /send <nom> home"),
    ("pause", "Aspirateur : pause — /pause <nom>"),
    ("state", "État et attributs d'une entité : /state <mot> (comme Developer tools → States)"),
    ("git", "Mettre à jour le code du Pi : /git pull"),
    ("restart", "Redémarrer le listener (relit config.json), sans passer par systemd"),
    ("rearm", "Alertes : la prochaine alerte de chaque détecteur sera envoyée (Telegram + SMS)"),
    ("arm", "Armer : /arm all, /arm <groupe> ou /arm <appareil>"),
    ("disarm", "Désarmer : /disarm all, /disarm <groupe> ou /disarm <appareil> (alias /unarm)"),
    ("stale", "Entités sans nouvelle depuis N h (24 par défaut) : appareils à nettoyer"),
    ("shutdown", "Éteindre le Pi (avec confirmation)"),
    ("stop", "stop listener|ping|monitor"),
    ("start", "start listener|ping|monitor"),
    ("id", "Identifiants du chat et de l'utilisateur (pour configurer un groupe)"),
    ("help", "Cette aide"),
]

# Droits : le chat privé du propriétaire (telegram_chat_id) a tous les droits. Dans un groupe de « telegram_groups »,
# « telegram_admins » ont tous les droits et « telegram_members » seulement ceux-ci ; les autres sont ignorés sans réponse.
GROUPS = {int(g) for g in CFG.get("telegram_groups", [])}
ADMINS = {int(u) for u in CFG.get("telegram_admins", [])}
if CHAT_ID.lstrip("-").isdigit() and int(CHAT_ID) > 0:
    ADMINS.add(int(CHAT_ID))  # un chat privé a pour identifiant celui de l'utilisateur : le propriétaire est administrateur partout
MEMBERS = {int(u) for u in CFG.get("telegram_members", [])}
MEMBER_COMMANDS = {"help", "status", "update", "list", "state", "stale", "data", "arm", "launch", "send", "pause", "id"}


def sender_role(chat_id, user_id):
    """"admin", "member" ou None (à ignorer)."""
    if str(chat_id) == CHAT_ID:
        return "admin"
    if chat_id in GROUPS:
        if user_id in ADMINS:
            return "admin"
        if user_id in MEMBERS:
            return "member"
    return None


def member_allows(cmd, args):
    """Un membre peut lire, armer et piloter l'aspirateur ; jamais désarmer, éteindre, remettre la data à zéro, ni /on, /off,
    /rearm, /git, /stop, /start."""
    if cmd.lstrip("/") not in MEMBER_COMMANDS:
        return False
    return not (cmd == "/data" and args[:1] == ["reset"])


def help_text(role):
    if role == "admin":
        return HELP
    return "🛠 Commandes autorisées ici\n" + "\n".join(f"/{c} — {d}" for c, d in COMMANDS if c in MEMBER_COMMANDS)


HELP = "🛠 Commandes\n" + "\n".join(f"/{c} — {d}" for c, d in COMMANDS) + (
    "\n\nlistener : écoute Telegram (stop = mode éco, 1 appel / jour)"
    "\nping : heartbeat Healthchecks (stop = alerte après la grâce)"
    "\nmonitor : résumé du matin (stop = le cron tourne mais n'envoie rien)")


def ping_stamp(result, now):
    """Valeur de `last_ping` après une tentative. Un échec réseau est retenté dans PING_RETRY s : avec une période de
    4 h et 1 h de grâce, attendre le ping suivant (4 h) transformerait une coupure d'une minute en fausse alerte."""
    return now - PING_EVERY + PING_RETRY if result is False else now


def code_signature():
    """Dates de modification de tous les .py : le listener importe arm, summary, vacuum… et doit redémarrer si l'un change."""
    return tuple(sorted((p.name, p.stat().st_mtime) for p in HERE.glob("*.py")))


def syntax_error():
    """None si tout le code se compile, sinon le message d'erreur (pour ne pas redémarrer sur un code cassé)."""
    files = [str(p) for p in sorted(HERE.glob("*.py"))]
    r = subprocess.run([sys.executable, "-m", "py_compile", *files], capture_output=True, text=True, timeout=60)
    return None if r.returncode == 0 else (r.stderr or r.stdout).strip()[-600:]


def git_pull():
    """/git pull : git pull --ff-only sur la branche courante, comme run.sh. Retourne le texte de réponse."""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}

    def git(*args, timeout=90):
        return subprocess.run(["git", *args], cwd=HERE, capture_output=True, text=True, timeout=timeout, env=env)

    branch = git("rev-parse", "--abbrev-ref", "HEAD", timeout=15).stdout.strip()
    if not branch or branch == "HEAD":
        return "❌ Dépôt en HEAD détaché : pas de git pull automatique."
    old = git("rev-parse", "--short", "HEAD", timeout=15).stdout.strip()
    r = git("pull", "-q", "--ff-only", "origin", branch)
    if r.returncode != 0:
        return f"❌ git pull a échoué ({branch}) :\n{(r.stderr or r.stdout).strip()[-500:]}"
    new = git("rev-parse", "--short", "HEAD", timeout=15).stdout.strip()
    if old == new:
        return f"✅ Déjà à jour ({new}, {branch})."
    log = git("log", "--oneline", "-8", f"{old}..{new}", timeout=15).stdout.strip()
    changed = git("diff", "--name-only", old, new, timeout=15).stdout.split()
    text = f"✅ Mis à jour {old} → {new} ({branch})\n{log}"
    if any(f.endswith(".py") for f in changed):
        text += "\n🔄 Le listener redémarre dans quelques secondes pour charger le nouveau code."
    return text


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


def restart(tg, offset):
    """Exécuté APRÈS l'envoi de la réponse : relit config.json et le code en se relançant, sans dépendre de systemd."""
    print("redémarrage demandé par /restart", flush=True)
    try:  # acquitter la commande : sinon le nouveau processus la recevrait de nouveau
        tg.call("getUpdates", offset=offset, timeout=0)
    except Exception as e:
        print(f"acquittement Telegram KO : {e}", file=sys.stderr)
    try:
        log_net("listener")
        pi_cum()
    except Exception as e:
        print(f"enregistrement data KO : {e}", file=sys.stderr)
    os.execv(sys.executable, [sys.executable, str(ME)])


def power_off(tg, offset):
    """Exécuté APRÈS l'envoi de l'acquittement."""
    st = load_state()
    print(f"arrêt : acquittement, signal Healthchecks ({SHUTDOWN_SIGNAL}), dry_run={SHUTDOWN_DRY_RUN}", flush=True)
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
    print(f"arrêt : signal envoyé={signalled}, data enregistrée, attente {SHUTDOWN_DELAY} s", flush=True)
    time.sleep(SHUTDOWN_DELAY)
    if SHUTDOWN_DRY_RUN:  # tout a été exécuté sauf l'extinction : on remet le check au vert
        if signalled:
            heartbeat()
        tg.reply("🧪 Simulation (shutdown.dry_run) : le Pi n'a PAS été éteint. Acquittement, signal Healthchecks "
                 "et enregistrement de la data ont bien été exécutés.")
        return
    cmd = ["systemctl", "poweroff"] if os.geteuid() == 0 else ["sudo", "-n", "systemctl", "poweroff"]
    print(f"arrêt : exécution de {' '.join(cmd)}", flush=True)
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print(f"arrêt : échec (code {r.returncode}) : {(r.stderr or r.stdout).strip()[:200]}", flush=True)  # le Pi tourne toujours : on annule l'échec signalé
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


def handle(text, st, role="admin"):
    parts = text.lower().split()
    cmd = parts[0].split("@")[0] if parts else ""
    args = parts[1:]
    if cmd == "/restart":
        problem = syntax_error()
        if problem:
            return f"❌ Redémarrage annulé : le code ne se compile pas.\n{problem}"
        return "🔄 Le listener redémarre (config.json et code relus). Il répond de nouveau dans quelques secondes.", restart
    if cmd == "/stop" and args[:1] == ["pi"]:
        cmd, args = "/shutdown", args[1:]
    if cmd == "/shutdown":
        if args[:1] != ["confirm"]:
            if not can_poweroff():
                print("/shutdown refusé : droit sudo absent (setup-power.sh)", flush=True)
                return "❌ Extinction non autorisée : lancez d'abord sudo ./setup-power.sh sur le Pi."
            PENDING["t"] = time.monotonic()
            return (f"⚠️ Éteindre le Pi ?{' (SIMULATION : dry_run actif, il ne sera pas éteint)' if SHUTDOWN_DRY_RUN else ''}\n• signal Healthchecks : {shutdown_signal_text(st)}\n"
                    "• il ne redémarrera pas seul : il faudra remettre le courant (ou le débrancher/rebrancher)\n\n"
                    f"Confirmez dans les {CONFIRM_WINDOW} s : /shutdown confirm")
        t = PENDING["t"]
        PENDING["t"] = None
        if t is None or time.monotonic() - t > CONFIRM_WINDOW:
            return "Aucune demande d'extinction en attente (ou expirée). Refaites /shutdown."
        return (f"{'🧪 Simulation' if SHUTDOWN_DRY_RUN else '⏻ Extinction'} dans {SHUTDOWN_DELAY} s.\nSignal Healthchecks : {shutdown_signal_text(st)}.\n"
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
        return help_text(role)
    if cmd == "/data":
        if parts[1:2] == ["reset"]:
            before = data_compact()
            log_net("listener")  # les octets en cours restent dans l'ancien cycle
            reset_cycle()
            return f"✅ Compteur remis à zéro\nAvant : {before}\n\n{data_block()}"
        return data_block()
    try:
        if cmd in ("/on", "/off"):
            return control.act(cmd == "/on", " ".join(parts[1:]) or None)
        if cmd == "/state":
            return summary.state_text(" ".join(parts[1:])) if parts[1:] else "Usage : /state <mot> (ex. /state living room camera)"
        if cmd == "/git":
            if [a.lower() for a in parts[1:]] != ["pull"]:
                return "Seul « /git pull » est permis : mise à jour du code sur le Pi."
            return git_pull()
        if cmd == "/rearm":
            return alerts.rearm()
        if cmd in vacuum.ACTIONS:
            return vacuum.act(cmd, " ".join(parts[1:]) or None)
        if cmd in ("/arm", "/disarm", "/unarm"):  # /unarm : alias de /disarm
            return arm.act(cmd == "/arm", " ".join(parts[1:]) or None)
        if cmd == "/stale":
            try:
                hours = float(parts[1]) if parts[1:2] else 24.0
            except ValueError:
                return "Usage : /stale [heures]   (24 par défaut)"
            return summary.stale_text(hours)
        if cmd == "/list":
            if parts[1:2] == ["all"]:
                return summary.list_flat_text()
            return summary.list_text(" ".join(parts[1:]) or None)
        if cmd == "/update":
            if parts[1:2] == ["all"]:
                return summary.all_states_text()
            return summary.all_states_text(" ".join(parts[1:])) if parts[1:] else summary.build()
    except Exception as e:
        return f"❌ Home Assistant : {e}"
    return f"❓ Commande inconnue : {text.split()[0] if parts else ''}\n\n" + HELP


def handle_message(msg, bot_username=None):
    """Un message Telegram -> (texte, suite, chat_id de la réponse), ou None s'il faut l'ignorer."""
    text = msg.get("text")
    words = text.split() if text else []
    if not words:
        return None
    chat_id = msg.get("chat", {}).get("id")
    user_id = msg.get("from", {}).get("id")
    first = words[0]
    if "@" in first and bot_username and first.split("@", 1)[1].lower() != bot_username:
        return None  # commande adressée à un autre bot du groupe
    cmd = first.split("@")[0].lower()
    if not cmd.startswith("/") and str(chat_id) != CHAT_ID:
        return None  # une conversation de groupe qui n'est pas une commande ne nous concerne pas
    role = sender_role(chat_id, user_id)
    if cmd == "/id":  # utile avant de configurer un groupe : il n'est pas encore autorisé
        print(f"/id demandé : chat {chat_id}, utilisateur {user_id}", flush=True)
        return (f"chat_id = {chat_id}\nuser_id = {user_id}\nrôle ici : {role or 'non autorisé'}", None, chat_id)
    if role is None:
        print(f"message ignoré (non autorisé) : chat {chat_id}, utilisateur {user_id}, commande {cmd}", flush=True)
        return None  # on n'obéit qu'au propriétaire et aux groupes configurés, sans répondre aux autres
    if role == "member" and not member_allows(cmd, [w.lower() for w in words[1:]]):
        print(f"commande refusée ({cmd}) : membre {user_id}", flush=True)
        return ("⛔ Cette commande est réservée aux administrateurs.", None, chat_id)
    print(f"commande reçue ({role}, chat {chat_id}, utilisateur {user_id}) : {text[:40]}", flush=True)
    out = handle(text, load_state(), role)
    reply, after = out if isinstance(out, tuple) else (out, None)
    return reply, after, chat_id


def loop():
    tg = Telegram()
    try:
        bot_username = (tg.call("getMe").get("result", {}).get("username") or "").lower() or None
    except Exception as e:
        bot_username = None
        print(f"getMe KO : {e}", file=sys.stderr)
    offset = 0
    last_ping = last_poll = last_sample = 0.0
    last_log_day = datetime.now().day
    signature = code_signature()
    warned = None
    print("listener démarré", flush=True)
    print(f"Telegram : chat privé {CHAT_ID} · groupes autorisés {sorted(GROUPS)} · administrateurs {sorted(ADMINS)} · membres {sorted(MEMBERS)}",
          flush=True)
    arm.start_timers()  # retour automatique des /arm et /disarm temporaires
    smsfwd.start()  # thread : SMS reçus par le routeur -> Telegram, indépendant de la page smsweb
    alerts.start()  # thread : première alerte du jour de chaque détecteur (si « alerts » est configuré)
    try:  # menu "/" de Telegram ; ~0,6 Ko, une fois au démarrage
        tg.call("setMyCommands", commands=json.dumps([{"command": c, "description": d} for c, d in COMMANDS]))
        for gid in GROUPS:  # dans un groupe, le menu ne propose que ce que les membres peuvent faire
            tg.call("setMyCommands", scope=json.dumps({"type": "chat", "chat_id": gid}),
                    commands=json.dumps([{"command": c, "description": d} for c, d in COMMANDS if c in MEMBER_COMMANDS]))
    except Exception as e:
        print(f"setMyCommands KO : {e}", file=sys.stderr)
    while True:
        st = load_state()
        now = time.time()

        if now - last_ping >= PING_EVERY:
            last_ping = ping_stamp(heartbeat(), now)  # ne fait rien si /stop ping

        if now - last_sample >= 3600:  # compteur du Pi : une lecture par heure (il repart à 0 au reboot)
            pi_cum()
            last_sample = now

        if datetime.now().day != last_log_day:  # une ligne de data par jour
            log_net("listener")
            last_log_day = datetime.now().day

        if code_signature() != signature:  # mis à jour par le git pull du matin ou par /git pull
            problem = syntax_error()
            if problem:  # un code cassé ne doit pas remplacer le listener qui tourne : on garde l'ancien et on prévient
                if problem != warned:
                    warned = problem
                    print(f"code mis à jour mais invalide, pas de redémarrage : {problem}", file=sys.stderr, flush=True)
                    try:
                        tg.reply(f"⚠️ Code mis à jour mais invalide : le listener garde l'ancienne version.\n{problem}")
                    except Exception:
                        pass
            else:
                print("code mis à jour, redémarrage", flush=True)
                try:  # acquitter les commandes déjà traitées (/git pull…) : sinon le nouveau processus les recevrait de nouveau
                    tg.call("getUpdates", offset=offset, timeout=0)
                except Exception:
                    pass
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
            result = handle_message(upd.get("message", {}), bot_username)
            if result is None:
                continue
            text, after, chat_id = result
            sent = tg.reply(text, chat_id=chat_id)
            if not sent.get("ok"):  # ex. le bot n'a pas le droit d'écrire dans ce groupe
                print(f"réponse non envoyée à {chat_id} : {sent.get('description')}", file=sys.stderr, flush=True)
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
