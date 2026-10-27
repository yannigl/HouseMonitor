"""Page web locale pour envoyer et recevoir des SMS par le routeur 4G (TL-MR6400), indépendante de Telegram.

config.json -> "smsweb": {"port": 8081, "host": "0.0.0.0", "max_per_day": 10, "poll_seconds": 300,
                          "contacts": [{"name": "Marie", "number": "+33612345678"}]}
  - envoi : seulement vers un contact enregistré (jamais un numéro saisi), `max_per_day` envois par jour (compteur « web »,
    séparé de celui des alertes du bot) ;
  - réception : la boîte du routeur est lue toutes les `poll_seconds` s (une connexion au routeur à chaque lecture : un seul
    administrateur à la fois, voir routersms.py) ; les messages sont gardés dans sms_inbox.json (ignoré par git) ;
  - aucun mot de passe, par choix : à utiliser sur le réseau local seulement, ne jamais l'ouvrir sur internet. Les SMS reçus
    (codes de confirmation compris) sont visibles par tout appareil du réseau local ;
  - se relance tout seul quand un .py du dépôt change (après vérification que le code se compile).
Lancement : python3 smsweb.py  (service : housemonitor-smsweb.service)
"""
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import routersms
from common import CFG, HERE

CONF = CFG.get("smsweb", {})
PORT = CONF.get("port", 8081)
BIND = CONF.get("host", "0.0.0.0")
POLL = max(60, CONF.get("poll_seconds", 300))
CONTACTS = [c for c in CONF.get("contacts", []) if c.get("name") and c.get("number")]
INBOX_FILE = HERE / "sms_inbox.json"
KEEP = 500          # messages gardés
MIN_REFRESH = 30    # s entre deux lectures du routeur déclenchées depuis la page
LOCK = threading.Lock()
STATE = {"last_poll": 0.0, "ok": None, "detail": "pas encore lu"}


def load_inbox():
    try:
        return json.loads(INBOX_FILE.read_text())
    except (OSError, ValueError):
        return []


def msg_key(m):
    return hashlib.sha1(f"{m.get('from')}|{m.get('receivedTime')}|{m.get('content')}".encode()).hexdigest()[:16]


def poll():
    """Lit la boîte du routeur et ajoute les nouveaux messages au fichier local. -> (ok, détail)."""
    with LOCK:
        STATE["last_poll"] = time.time()
        ok, res = routersms.read_inbox()
        if not ok:
            STATE.update(ok=False, detail=res)
            return False, res
        known = load_inbox()
        keys = {m["key"] for m in known}
        new = [{**m, "key": msg_key(m)} for m in res if msg_key(m) not in keys]
        if new:
            INBOX_FILE.write_text(json.dumps((known + new)[-KEEP:], ensure_ascii=False))
        STATE.update(ok=True, detail=f"{len(new)} nouveau(x) message(s)")
        return True, STATE["detail"]


def poll_loop():
    time.sleep(5)
    while True:
        try:
            poll()
        except Exception as e:  # ne jamais arrêter la lecture périodique
            STATE.update(ok=False, detail=f"{type(e).__name__}: {e}")
        time.sleep(POLL)


def name_of(number):
    """Nom du contact dont le numéro se termine par les mêmes 9 chiffres."""
    tail = "".join(ch for ch in str(number) if ch.isdigit())[-9:]
    for c in CONTACTS:
        if tail and "".join(ch for ch in c["number"] if ch.isdigit())[-9:] == tail:
            return c["name"]
    return None


def api_state():
    msgs = sorted(load_inbox(), key=lambda m: str(m.get("receivedTime")), reverse=True)[:200]
    return {
        "contacts": [{"name": c["name"], "number": c["number"]} for c in CONTACTS],
        "sent_today": routersms.sent_today("web"),
        "limit": routersms.LIMITS["web"](),
        "inbox": [{"from": m.get("from"), "name": name_of(m.get("from")), "content": m.get("content"),
                   "time": m.get("receivedTime"), "unread": bool(m.get("unread"))} for m in msgs],
        "poll": {"ok": STATE["ok"], "detail": STATE["detail"], "at": STATE["last_poll"], "every": POLL},
    }


PAGE = """<!doctype html>
<html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>SMS du routeur</title>
<style>
 body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:46rem;margin:1rem auto;padding:0 1rem;color:#1b1b1b;background:#fafafa}
 h1{font-size:1.3rem} h2{font-size:1.05rem;margin-top:1.6rem}
 select,textarea,button{font:inherit;width:100%;box-sizing:border-box;margin:.3rem 0;padding:.55rem;border:1px solid #bbb;border-radius:.4rem}
 button{background:#0b6bcb;color:#fff;border:0;cursor:pointer} button:disabled{background:#9ab}
 .small{color:#666;font-size:.85rem} .ok{color:#0a7a2f} .ko{color:#b3261e}
 .msg{background:#fff;border:1px solid #ddd;border-radius:.5rem;padding:.6rem .8rem;margin:.5rem 0}
 .msg.new{border-left:4px solid #d93025} .from{font-weight:600} .date{float:right}
 .txt{white-space:pre-wrap;word-break:break-word;margin-top:.3rem}
</style></head><body>
<h1>SMS du routeur</h1>
<h2>Envoyer</h2>
<select id="contact"></select>
<textarea id="text" rows="4" maxlength="300" placeholder="Message (300 caractères max)"></textarea>
<div class="small"><span id="count">0</span>/300 · <span id="quota"></span></div>
<button id="send">Envoyer</button>
<div id="result" class="small"></div>
<h2>Reçus</h2>
<div class="small" id="poll"></div>
<button id="refresh" style="width:auto;padding:.4rem .9rem">Relire la boîte maintenant</button>
<div id="inbox"></div>
<script>
const $ = id => document.getElementById(id);
const H = {"Content-Type": "application/json", "X-SMSWeb": "1"};
function fmt(ts){ return ts ? new Date(ts*1000).toLocaleTimeString("fr-FR") : "—"; }
async function load(){
  try {
    const s = await (await fetch("/api/state")).json();
    const sel = $("contact"), cur = sel.value;
    sel.innerHTML = "";
    s.contacts.forEach((c,i) => { const o = document.createElement("option"); o.value = i; o.textContent = c.name + " (" + c.number + ")"; sel.appendChild(o); });
    if (cur) sel.value = cur;
    $("quota").textContent = s.sent_today + " envoyé(s) aujourd'hui sur " + s.limit;
    $("send").disabled = !s.contacts.length || s.sent_today >= s.limit;
    const p = s.poll;
    $("poll").textContent = (p.ok === false ? "Dernière lecture en erreur : " : "Dernière lecture " + fmt(p.at) + " : ") + p.detail + " · relecture automatique toutes les " + Math.round(p.every/60) + " min";
    $("poll").className = "small " + (p.ok === false ? "ko" : "");
    const box = $("inbox"); box.innerHTML = "";
    if (!s.inbox.length) box.innerHTML = '<div class="small">Aucun message reçu.</div>';
    s.inbox.forEach(m => {
      const d = document.createElement("div"); d.className = "msg" + (m.unread ? " new" : "");
      const f = document.createElement("span"); f.className = "from"; f.textContent = m.name ? m.name + " (" + m.from + ")" : m.from;
      const t = document.createElement("span"); t.className = "date small"; t.textContent = m.time;
      const x = document.createElement("div"); x.className = "txt"; x.textContent = m.content;
      d.append(t, f, x); box.appendChild(d);
    });
  } catch(e) { $("poll").textContent = "Page injoignable : " + e; }
}
$("text").addEventListener("input", () => $("count").textContent = $("text").value.length);
$("send").addEventListener("click", async () => {
  const text = $("text").value.trim(); if (!text) return;
  const name = $("contact").selectedOptions[0].textContent;
  if (!confirm("Envoyer à " + name + " ?\\n\\n" + text)) return;
  $("send").disabled = true; $("result").className = "small"; $("result").textContent = "Envoi en cours (jusqu'à une minute)…";
  try {
    const r = await (await fetch("/api/send", {method: "POST", headers: H, body: JSON.stringify({contact: +$("contact").value, text})})).json();
    $("result").className = "small " + (r.ok ? "ok" : "ko"); $("result").textContent = (r.ok ? "✅ " : "❌ ") + r.detail;
    if (r.ok) { $("text").value = ""; $("count").textContent = 0; }
  } catch(e) { $("result").className = "small ko"; $("result").textContent = "❌ " + e; }
  load();
});
$("refresh").addEventListener("click", async () => {
  $("refresh").disabled = true;
  try { await fetch("/api/refresh", {method: "POST", headers: H}); } catch(e) {}
  $("refresh").disabled = false; load();
});
load(); setInterval(load, 30000);
</script></body></html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "smsweb"

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _same_origin(self):
        """Protège sans mot de passe contre une page tierce qui ferait envoyer un SMS : en-tête propre à notre page, et origine
        identique si le navigateur en fournit une."""
        if self.headers.get("X-SMSWeb") != "1":
            return False
        origin = self.headers.get("Origin")
        return not origin or urlparse(origin).netloc == self.headers.get("Host")

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            body = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/state":
            self._json(api_state())
        else:
            self._json({"error": "introuvable"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        if not self._same_origin():
            return self._json({"ok": False, "detail": "requête refusée (origine)"}, 403)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            data = json.loads(self.rfile.read(min(n, 10_000)) or b"{}")
        except (ValueError, OSError):
            return self._json({"ok": False, "detail": "requête illisible"}, 400)
        if path == "/api/send":
            text = str(data.get("text", "")).strip()
            try:
                contact = CONTACTS[int(data["contact"])]
            except (KeyError, ValueError, IndexError, TypeError):
                return self._json({"ok": False, "detail": "contact inconnu"}, 400)
            if not text or len(text) > 300:
                return self._json({"ok": False, "detail": "message vide ou trop long (300 caractères)"}, 400)
            print(f"envoi demandé à {contact['name']} ({len(text)} caractères)", flush=True)
            ok, detail = routersms.send(text, contact["number"], bucket="web")
            return self._json({"ok": ok, "detail": detail})
        if path == "/api/refresh":
            if time.time() - STATE["last_poll"] < MIN_REFRESH:
                return self._json({"ok": False, "detail": "lecture trop récente, réessayez dans un instant"})
            ok, detail = poll()
            return self._json({"ok": ok, "detail": detail})
        self._json({"error": "introuvable"}, 404)

    def log_message(self, fmt, *args):  # pas de ligne de journal par requête (la page se rafraîchit toute seule)
        pass


def code_signature():
    return tuple(sorted((p.name, p.stat().st_mtime) for p in HERE.glob("*.py")))


def watch_code(signature):
    """Se relance quand un .py du dépôt change (git pull), sauf si le nouveau code ne se compile pas."""
    warned = False
    while True:
        time.sleep(20)
        if code_signature() == signature:
            continue
        files = [str(p) for p in sorted(HERE.glob("*.py"))]
        if subprocess.run([sys.executable, "-m", "py_compile", *files], capture_output=True).returncode != 0:
            if not warned:
                print("code mis à jour mais invalide : smsweb garde l'ancienne version", file=sys.stderr, flush=True)
                warned = True
            continue
        print("code mis à jour, redémarrage", flush=True)
        os.execv(sys.executable, [sys.executable, str(HERE / "smsweb.py")])


def main():
    if not CONTACTS:
        print("smsweb : aucun contact dans config.json → smsweb.contacts (l'envoi sera désactivé)", file=sys.stderr, flush=True)
    threading.Thread(target=poll_loop, daemon=True, name="poll").start()
    threading.Thread(target=watch_code, args=(code_signature(),), daemon=True, name="watch").start()
    server = ThreadingHTTPServer((BIND, PORT), Handler)
    print(f"smsweb : http://{BIND}:{PORT} ({len(CONTACTS)} contact(s), {routersms.LIMITS['web']()} envois par jour max)", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
