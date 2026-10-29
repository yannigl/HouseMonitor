"""Page web locale pour envoyer des SMS par le routeur 4G (TL-MR6400) depuis la maison, indépendante de Telegram.

config.json -> "smsweb": {"port": 8081, "host": "0.0.0.0", "max_per_day": 10, "show_inbox": true,
                          "contacts": [{"name": "Marie", "number": "+33612345678"}]}
  - envoi : seulement vers un contact enregistré (jamais un numéro saisi), `max_per_day` envois par jour (compteur « web »,
    séparé de celui des alertes du bot) ;
  - cette page ne lit PAS le routeur : la boîte de réception est lue par smsfwd.py (dans le listener), qui transmet aussi les SMS à
    Telegram, même si cette page est arrêtée. La page affiche seulement le fichier sms_inbox.json que smsfwd écrit ;
    `"show_inbox": false` masque les messages reçus sur la page (par exemple pour que les enfants n'y voient que l'envoi) ;
  - aucun mot de passe, par choix : à utiliser sur le réseau local seulement, ne jamais l'ouvrir sur internet ;
  - se relance tout seul quand un .py du dépôt change (après vérification que le code se compile).
Lancement : python3 smsweb.py  (service : housemonitor-smsweb.service)
"""
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import routersms
import smsstore
from common import CFG, HERE

CONF = CFG.get("smsweb", {})
PORT = CONF.get("port", 8081)
BIND = CONF.get("host", "0.0.0.0")
SHOW_INBOX = CONF.get("show_inbox", True)
REFRESH = max(5, CONF.get("refresh_seconds", 15))  # la page relit le fichier local toutes les N s (aucun accès au routeur)
CONTACTS = smsstore.contacts()


def api_state():
    msgs = sorted(smsstore.load_inbox(), key=lambda m: str(m.get("receivedTime")), reverse=True)[:200] if SHOW_INBOX else []
    return {
        "contacts": [{"name": c["name"], "number": c["number"]} for c in CONTACTS],
        "sent_today": routersms.sent_today("web"),
        "limit": routersms.LIMITS["web"](),
        "show_inbox": SHOW_INBOX,
        "refresh": REFRESH,
        "inbox": [{"from": m.get("from"), "name": smsstore.name_of(m.get("from")), "content": m.get("content"),
                   "time": m.get("receivedTime"), "unread": bool(m.get("unread")), "key": m.get("key")} for m in msgs],
        "poll": smsstore.read_status(),
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
<div id="received">
<h2>Reçus</h2>
<button id="sound" style="width:auto;padding:.4rem .9rem">🔕 Son désactivé — touchez pour activer</button>
<button id="testsound" style="width:auto;padding:.4rem .9rem;background:#6b7785;display:none">♪ Tester</button>
<div class="small" id="poll"></div>
<div id="inbox"></div>
</div>
<script>
const $ = id => document.getElementById(id);
// Sonnerie générée par le navigateur (pas de fichier) : mélodie « Gran Vals » de Tárrega, celle des vieux Nokia.
const TUNE = [[659.26,1],[587.33,1],[369.99,2],[415.30,2],[554.37,1],[493.88,1],[293.66,2],[329.63,2],[493.88,1],[440,1],[277.18,2],[329.63,2],[440,6]];
let audio = null, soundOn = false, seen = null, flashTimer = null;
try { soundOn = localStorage.getItem("sound") === "1"; } catch(e) {}
function ring(){
  if (!audio) { const C = window.AudioContext || window.webkitAudioContext; if (!C) return; audio = new C(); }
  if (audio.state === "suspended") audio.resume();
  let t = audio.currentTime + 0.05; const unit = 0.11;
  TUNE.forEach(([f, n]) => {
    const o = audio.createOscillator(), g = audio.createGain();
    o.type = "square"; o.frequency.value = f;
    g.gain.setValueAtTime(0.0001, t); g.gain.exponentialRampToValueAtTime(0.18, t + 0.01);
    g.gain.setValueAtTime(0.18, t + n*unit - 0.03); g.gain.exponentialRampToValueAtTime(0.0001, t + n*unit);
    o.connect(g); g.connect(audio.destination); o.start(t); o.stop(t + n*unit + 0.02); t += n*unit;
  });
}
function paintSound(){ $("sound").textContent = soundOn ? "🔔 Son activé" : "🔕 Son désactivé — touchez pour activer"; $("testsound").style.display = soundOn ? "" : "none"; }
$("sound").addEventListener("click", () => { soundOn = !soundOn; try { localStorage.setItem("sound", soundOn ? "1" : "0"); } catch(e) {} if (soundOn) ring(); paintSound(); });
$("testsound").addEventListener("click", ring);
paintSound();
function announce(n){
  if (soundOn) ring();
  const base = "SMS du routeur"; let on = true;
  clearInterval(flashTimer);
  flashTimer = setInterval(() => { document.title = on ? "🔔 " + n + " nouveau(x) SMS" : base; on = !on; }, 1000);
}
document.addEventListener("click", () => { clearInterval(flashTimer); document.title = "SMS du routeur"; });
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
    $("received").style.display = s.show_inbox ? "" : "none";
    const p = s.poll;
    $("poll").textContent = (p.ok === false ? "Dernière lecture en erreur : " : "Dernière lecture " + fmt(p.at) + " : ") + p.detail + (p.every ? " · relecture toutes les " + Math.round(p.every/60) + " min" : "");
    $("poll").className = "small " + (p.ok === false ? "ko" : "");
    const box = $("inbox"); box.innerHTML = "";
    if (!s.inbox.length) box.innerHTML = '<div class="small">Aucun message reçu.</div>';
    const keys = s.inbox.map(m => m.key);
    if (seen === null) { seen = new Set(keys); }   // premier affichage : l'existant ne sonne pas
    const fresh = keys.filter(k => k && !seen.has(k));
    if (fresh.length) { fresh.forEach(k => seen.add(k)); announce(fresh.length); }
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
load(); fetch("/api/state").then(r => r.json()).then(s => setInterval(load, (s.refresh || 15) * 1000)).catch(() => setInterval(load, 15000));
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
    threading.Thread(target=watch_code, args=(code_signature(),), daemon=True, name="watch").start()
    try:
        server = ThreadingHTTPServer((BIND, PORT), Handler)
    except PermissionError:
        sys.exit(f"smsweb : le port {PORT} est privilégié. Sous systemd, installez la version à jour de housemonitor-smsweb.service "
                 "(AmbientCapabilities=CAP_NET_BIND_SERVICE), ou choisissez un port ≥ 1024 (smsweb.port).")
    except OSError as e:
        sys.exit(f"smsweb : impossible d'écouter sur {BIND}:{PORT} ({e}). Un autre programme utilise peut-être ce port : sudo ss -ltnp | grep ':{PORT} '")
    print(f"smsweb : http://{BIND}:{PORT} ({len(CONTACTS)} contact(s), {routersms.LIMITS['web']()} envois par jour max)", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
