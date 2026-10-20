"""Envoi de SMS par le routeur 4G TP-Link TL-MR6400 (interface web, firmware 1.0.x).

config.json -> "sms": {"host": "192.168.1.1", "user": "yannig", "password": "...", "to": "+33612345678", "max_per_day": 5}

Protocole (lu dans l'interface web du routeur, pas de documentation officielle) :
  1. cookie « Authorization=escape("Basic " + base64(user:md5(motdepasse)))» puis GET /userRpm/LoginRpm.htm?Save=Save ;
     la réponse contient le jeton de session : /<16 caractères>/userRpm/
  2. POST /<jeton>/userRpm/lteWebCfg, corps JSON {"module": "message", "action": 3, "sendMessage": {to, textContent, sendTime}}
  3. action 7 : état de l'envoi (0 = envoyé, 4 = en cours)
  4. GET /<jeton>/userRpm/LogoutRpm.htm
Le routeur n'accepte qu'un administrateur à la fois (se connecter déconnecte l'interface web) et bloque la connexion deux heures
après dix échecs : après un échec de connexion, plus aucune tentative pendant BLOCK_AFTER_FAIL s. Au plus `max_per_day` SMS par jour.

Test depuis le Pi :  python3 routersms.py "texte du SMS"
"""
import base64
import hashlib
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime

from common import CFG, HERE

CONF = CFG.get("sms", {})
STATE = HERE / "sms_state.json"
BLOCK_AFTER_FAIL = 3600  # s sans nouvelle tentative après un échec de connexion
SEND_STATUS = {0: "envoyé", 1: "envoyé mais non enregistré", 2: "non envoyé (enregistré en brouillon)",
               3: "non envoyé", 4: "en cours"}


def _state():
    try:
        st = json.loads(STATE.read_text())
    except (OSError, ValueError):
        st = {}
    today = datetime.now().strftime("%Y-%m-%d")
    if st.get("day") != today:
        st = {"day": today, "count": 0, "blocked_until": st.get("blocked_until", 0)}
    return st


def _save(st):
    STATE.write_text(json.dumps(st))


class Router:
    def __init__(self, host, user, password):
        self.host = host
        md5 = hashlib.md5(password.encode()).hexdigest()
        auth = "Basic " + base64.b64encode(f"{user}:{md5}".encode()).decode()
        self.cookie = "Authorization=" + urllib.parse.quote(auth, safe="@*_+-./")  # = escape() de JavaScript
        self.base = None

    def _req(self, path, body=None):
        headers = {"Cookie": self.cookie, "Referer": f"http://{self.host}/", "User-Agent": "Mozilla/5.0"}
        if body is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
            body = body.encode()
        with urllib.request.urlopen(urllib.request.Request(f"http://{self.host}{path}", data=body, headers=headers),
                                    timeout=20) as r:
            return r.read().decode("utf-8", "replace")

    def login(self):
        page = self._req("/userRpm/LoginRpm.htm?Save=Save")
        m = re.search(r"/([A-Za-z0-9]{16})/userRpm/", page)
        if not m:
            raise PermissionError("connexion refusée (mot de passe, session admin déjà ouverte ou trop d'essais)")
        self.base = f"/{m.group(1)}/userRpm/"

    def call(self, action, data=None):
        body = json.dumps({"module": "message", "action": action, **(data or {})})
        return json.loads(self._req(self.base + "lteWebCfg", body))

    def logout(self):
        try:
            self._req(self.base + "LogoutRpm.htm")
        except Exception:
            pass


def send(text, to=None):
    """Envoie un SMS. Retourne (ok, détail). Ne lève jamais d'exception."""
    to = to or CONF.get("to")
    if not (CONF.get("host") and CONF.get("user") and CONF.get("password") and to):
        return False, "sms non configuré (host, user, password, to)"
    if not re.fullmatch(r"\+?\d{1,19}", to):
        return False, f"numéro invalide : {to}"
    st = _state()
    if time.time() < st.get("blocked_until", 0):
        return False, "envoi suspendu après un échec de connexion au routeur (réessai dans moins d'une heure)"
    if st["count"] >= CONF.get("max_per_day", 5):
        return False, f"limite de {CONF.get('max_per_day', 5)} SMS par jour atteinte"
    router = Router(CONF["host"], CONF["user"], CONF["password"])
    try:
        try:
            try:
                router.login()
            except PermissionError:
                time.sleep(10)  # le routeur refuse parfois une connexion juste après une session précédente
                router.login()  # un seul nouvel essai : 10 échecs bloquent le routeur 2 h
        except PermissionError as e:
            st["blocked_until"] = time.time() + BLOCK_AFTER_FAIL
            _save(st)
            return False, str(e)
        sent_at = ",".join(str(x) for x in (lambda d: (d.year, d.month, d.day, d.hour, d.minute, d.second))(datetime.now()))
        r = router.call(3, {"sendMessage": {"to": to, "textContent": text[:300], "sendTime": sent_at}})
        if r.get("result") != 0:
            return False, f"le routeur a refusé l'envoi (résultat {r.get('result')})"
        st["count"] += 1
        _save(st)
        for _ in range(20):  # l'envoi est asynchrone : on attend le verdict du routeur
            time.sleep(3)
            res = router.call(7).get("result")
            if res != 4:
                return res == 0, SEND_STATUS.get(res, f"résultat {res}")
        return False, "délai dépassé : état de l'envoi inconnu"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    finally:
        if router.base:
            router.logout()  # libère la session d'administration


if __name__ == "__main__":
    ok, detail = send(" ".join(sys.argv[1:]) or "Test HouseMonitor")
    print(("✅ " if ok else "❌ ") + detail)
    sys.exit(0 if ok else 1)
