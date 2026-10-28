"""Transmission à Telegram des SMS reçus par le routeur 4G : aucun SMS perdu quand vous n'êtes pas là.
Tourne dans un thread du listener, indépendamment de la page web (smsweb.py) : la page peut être arrêtée ou absente.

config.json -> "smsfwd": {"enabled": true, "poll_seconds": 300, "to_notify_chats": false, "skip": ["code", "otp"]}
  - enabled : transmission active (vrai par défaut dès que le bloc « sms » du routeur est configuré) ;
  - poll_seconds : lecture de la boîte du routeur (60 au minimum : c'est aussi le délai avant Telegram). Chaque lecture se connecte au
    routeur comme administrateur (un seul à la fois, voir routersms.py) ;
  - to_notify_chats : aussi aux chats de telegram_notify_chats (sinon : votre chat privé seulement) ;
  - skip : mots (expéditeur ou texte) qui empêchent la transmission, ex. les codes de confirmation, qui sinon passent par les
    serveurs de Telegram. Ces messages restent dans sms_inbox.json.
Rien n'est marqué lu ni supprimé sur le routeur. Au premier lancement, l'historique de la boîte n'est pas transmis. Au plus 5 messages
par lecture (le reste à la lecture suivante) ; un échec d'envoi Telegram est retenté à la lecture suivante.
Anciens réglages « smsweb.forward_telegram / forward_to_notify_chats / forward_skip / poll_seconds » repris s'il n'y a pas de bloc smsfwd.
"""
import sys
import threading
import time

import routersms
import smsstore
import summary
from common import CFG

_OLD = CFG.get("smsweb", {})
CONF = CFG.get("smsfwd", {})
ENABLED = CONF.get("enabled", _OLD.get("forward_telegram", True))
POLL = max(60, CONF.get("poll_seconds", _OLD.get("poll_seconds", 300)))
ALL_CHATS = CONF.get("to_notify_chats", _OLD.get("forward_to_notify_chats", False))
SKIP = [w.lower() for w in CONF.get("skip", _OLD.get("forward_skip", []))]
MAX_PER_POLL = 5
LOCK = threading.Lock()


def forward_pending():
    """Envoie à Telegram les messages marqués « fwd »: False. Retourne le nombre de messages envoyés."""
    inbox = smsstore.load_inbox()
    sent = 0
    for m in sorted((m for m in inbox if m.get("fwd") is False), key=lambda m: str(m.get("receivedTime"))):
        blob = f"{m.get('from')} {m.get('content')}".lower()
        if any(w in blob for w in SKIP):
            m["fwd"] = "ignoré"
            continue
        if sent >= MAX_PER_POLL:
            break
        who = smsstore.name_of(m.get("from"))
        head = f"📩 SMS de {who} ({m.get('from')})" if who else f"📩 SMS de {m.get('from')}"
        try:
            summary.send(f"{head} — {m.get('receivedTime')}\n{str(m.get('content'))[:3500]}", owner_only=not ALL_CHATS)
        except Exception as e:
            print(f"SMS : transmission Telegram KO : {type(e).__name__}: {e}", file=sys.stderr, flush=True)
            break
        m["fwd"] = True
        sent += 1
    smsstore.save_inbox(inbox)
    return sent


def poll():
    """Lit la boîte du routeur, ajoute les nouveaux messages au fichier local, puis les transmet. -> (ok, détail)."""
    with LOCK:
        ok, res = routersms.read_inbox()
        if not ok:
            smsstore.write_status(False, res, POLL)
            return False, res
        first = not smsstore.INBOX_FILE.exists()  # premier lancement : l'historique n'est pas transmis
        known = smsstore.load_inbox()
        keys = {m.get("key") for m in known}
        new = [{**m, "key": smsstore.msg_key(m), "fwd": first or not ENABLED} for m in res if smsstore.msg_key(m) not in keys]
        if new or first:
            smsstore.save_inbox(known + new)
        sent = forward_pending() if ENABLED else 0
        detail = f"{len(new)} nouveau(x) message(s)" + (f", {sent} transmis à Telegram" if sent else "")
        smsstore.write_status(True, detail, POLL)
        return True, detail


def run():
    time.sleep(20)  # laisser le listener démarrer
    print(f"SMS : transmission à Telegram active, lecture toutes les {POLL} s", flush=True)
    while True:
        try:
            poll()
        except Exception as e:  # ne jamais arrêter la surveillance
            print(f"SMS : lecture KO : {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        time.sleep(POLL)


def start():
    """Démarre le thread si le routeur est configuré et la transmission activée."""
    sms = CFG.get("sms", {})
    if ENABLED and sms.get("host") and sms.get("user") and sms.get("password"):
        threading.Thread(target=run, daemon=True, name="sms-forward").start()
