"""Stockage partagé des SMS reçus : écrit par smsfwd.py (listener), lu par smsweb.py (page). Aucun accès au routeur ici.

sms_inbox.json : liste de messages {index, from, content, receivedTime, unread, key, fwd}, les 500 derniers (ignoré par git) ;
sms_poll.json  : résultat de la dernière lecture du routeur {ok, detail, at, every}.
"""
import hashlib
import json
import time

from common import CFG, HERE

INBOX_FILE = HERE / "sms_inbox.json"
STATUS_FILE = HERE / "sms_poll.json"
KEEP = 500


def contacts():
    return [c for c in CFG.get("smsweb", {}).get("contacts", []) if c.get("name") and c.get("number")]


def load_inbox():
    try:
        return json.loads(INBOX_FILE.read_text())
    except (OSError, ValueError):
        return []


def save_inbox(messages):
    INBOX_FILE.write_text(json.dumps(messages[-KEEP:], ensure_ascii=False))


def msg_key(m):
    return hashlib.sha1(f"{m.get('from')}|{m.get('receivedTime')}|{m.get('content')}".encode()).hexdigest()[:16]


def name_of(number):
    """Nom du contact dont le numéro se termine par les mêmes 9 chiffres."""
    tail = "".join(ch for ch in str(number) if ch.isdigit())[-9:]
    for c in contacts():
        if tail and "".join(ch for ch in c["number"] if ch.isdigit())[-9:] == tail:
            return c["name"]
    return None


def read_status():
    try:
        return json.loads(STATUS_FILE.read_text())
    except (OSError, ValueError):
        return {"ok": None, "detail": "pas encore lu (le listener lit la boîte du routeur)", "at": 0, "every": 0}


def write_status(ok, detail, every):
    STATUS_FILE.write_text(json.dumps({"ok": ok, "detail": detail, "at": time.time(), "every": every}))
