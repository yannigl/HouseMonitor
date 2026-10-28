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

Test depuis le Pi :  python3 routersms.py "texte du SMS"   ·   python3 routersms.py --probe-inbox (réception, sans afficher les messages)
"""
import base64
import contextlib
import fcntl
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
    """{"day", "counts": {seau: n}, "blocked_until"} ; les compteurs repartent à zéro chaque jour."""
    try:
        st = json.loads(STATE.read_text())
    except (OSError, ValueError):
        st = {}
    today = datetime.now().strftime("%Y-%m-%d")
    if st.get("day") != today:
        st = {"day": today, "counts": {}, "blocked_until": st.get("blocked_until", 0)}
    if "count" in st:  # ancien format : un seul compteur, celui des alertes
        st.setdefault("counts", {})["alerts"] = st.pop("count")
    st.setdefault("counts", {})
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
        raw = self._req(self.base + "lteWebCfg", body)
        try:
            return json.loads(raw)
        except ValueError:
            raise RuntimeError(f"réponse du routeur illisible (action {action}) : {raw[:120]!r}")

    def logout(self):
        try:
            self._req(self.base + "LogoutRpm.htm")
        except Exception:
            pass


LOCK_FILE = HERE / ".router.lock"
LIMITS = {"alerts": lambda: CONF.get("max_per_day", 5), "web": lambda: CFG.get("smsweb", {}).get("max_per_day", 10)}


class RouterBusy(Exception):
    """Le routeur ne peut pas être utilisé maintenant (connexion refusée, pause en cours, autre programme en cours)."""


@contextlib.contextmanager
def router_session(wait=90):
    """Une seule session d'administration à la fois sur le routeur : les programmes (bot, page web) passent l'un après l'autre
    grâce à un verrou de fichier. Se connecte (un nouvel essai après 10 s), puis se déconnecte toujours."""
    if not (CONF.get("host") and CONF.get("user") and CONF.get("password")):
        raise RouterBusy("sms non configuré (host, user, password)")
    st = _state()
    if time.time() < st.get("blocked_until", 0):
        raise RouterBusy("accès au routeur suspendu après un échec de connexion (réessai dans moins d'une heure)")
    with open(LOCK_FILE, "w") as lock:
        deadline = time.time() + wait
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.time() > deadline:
                    raise RouterBusy("le routeur est utilisé par un autre programme, réessayez")
                time.sleep(1)
        router = Router(CONF["host"], CONF["user"], CONF["password"])
        try:
            try:
                try:
                    router.login()
                except PermissionError:
                    time.sleep(10)  # le routeur refuse parfois une connexion juste après une session précédente
                    router.login()  # un seul nouvel essai : 10 échecs bloquent le routeur 2 h
            except PermissionError as e:
                st = _state()
                st["blocked_until"] = time.time() + BLOCK_AFTER_FAIL
                _save(st)
                raise RouterBusy(str(e))
            yield router
        finally:
            if router.base:
                router.logout()  # libère la session d'administration
            fcntl.flock(lock, fcntl.LOCK_UN)


def sent_today(bucket="alerts"):
    return _state()["counts"].get(bucket, 0)


def send(text, to=None, bucket="alerts"):
    """Envoie un SMS. `bucket` : « alerts » (le bot) ou « web » (la page) : chacun a son plafond quotidien.
    Retourne (ok, détail). Ne lève jamais d'exception."""
    to = to or CONF.get("to")
    if not to:
        return False, "sms non configuré (to)"
    if not re.fullmatch(r"\+?\d{1,19}", to):
        return False, f"numéro invalide : {to}"
    limit = LIMITS[bucket]()
    if sent_today(bucket) >= limit:
        return False, f"limite de {limit} SMS par jour atteinte"
    try:
        with router_session() as router:
            sent_at = ",".join(str(x) for x in (lambda d: (d.year, d.month, d.day, d.hour, d.minute, d.second))(datetime.now()))
            r = router.call(3, {"sendMessage": {"to": to, "textContent": text[:300], "sendTime": sent_at}})
            if r.get("result") != 0:
                return False, f"le routeur a refusé l'envoi (résultat {r.get('result')})"
            st = _state()
            st["counts"][bucket] = st["counts"].get(bucket, 0) + 1
            _save(st)
            for _ in range(20):  # l'envoi est asynchrone : on attend le verdict du routeur
                time.sleep(3)
                res = router.call(7).get("result")
                if res != 4:
                    return res == 0, SEND_STATUS.get(res, f"résultat {res}")
            return False, "délai dépassé : état de l'envoi inconnu"
    except RouterBusy as e:
        return False, str(e)
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


AMOUNTS = (8, 10, 5)  # « amountPerPage » : la page du routeur l'envoie avec la lecture ; sa valeur est dans une autre partie de son code


def _read_page(router, page, amount):
    return router.call(2, {"pageNumber": page, "amountPerPage": amount, "box": 0})  # box 0 = reçus (page du routeur)


def read_inbox(max_pages=10):
    """Lit la boîte de réception du routeur, sans rien marquer ni supprimer.
    -> (ok, messages ou détail) ; chaque message : {"index", "from", "content", "receivedTime", "unread"}."""
    out, seen = [], set()
    try:
        with router_session() as router:
            amount, first, last_error = None, None, None
            for candidate in AMOUNTS:  # la 1re valeur acceptée par le routeur sert pour toutes les pages
                try:
                    first = _read_page(router, 1, candidate)
                    amount = candidate
                    break
                except RuntimeError as e:  # réponse illisible : valeur refusée
                    last_error = e
            if amount is None:
                raise last_error
            page, r = 1, first
            while True:
                batch = r.get("messageList") or []
                fresh = [m for m in batch if m.get("index") not in seen]
                if not fresh:
                    break  # page vide, ou le routeur répète la dernière page
                seen.update(m.get("index") for m in fresh)
                out += fresh
                page += 1
                if page > max_pages or len(batch) < amount:
                    break
                r = _read_page(router, page, amount)
    except RouterBusy as e:
        return False, str(e)
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    return True, out


def probe_inbox_variants():
    """Essaie plusieurs formes de la requête de lecture dans UNE seule session et décrit les réponses (structure seulement,
    jamais le texte des messages). -> lignes de texte."""
    variants = [(f"amountPerPage={n}", {"pageNumber": 1, "amountPerPage": n, "box": 0}) for n in (8, 10, 5, 20)]
    variants.append(("sans amountPerPage", {"pageNumber": 1, "box": 0}))
    out = []
    with router_session() as router:
        for label, data in variants:
            try:
                r = router.call(2, data)
                lst = r.get("messageList")
                out.append(f"{label} : clés {sorted(r)}" + (f", {len(lst)} message(s)" if isinstance(lst, list) else ""))
            except Exception as e:
                out.append(f"{label} : {type(e).__name__}: {e}")
    return out


if __name__ == "__main__":
    if sys.argv[1:2] == ["--probe-inbox-variants"]:
        try:
            print("\n".join(probe_inbox_variants()))
        except Exception as e:
            print(f"❌ {type(e).__name__}: {e}")
            sys.exit(1)
        sys.exit(0)
    if sys.argv[1:2] == ["--probe-inbox"]:  # structure de la boîte de réception, sans afficher les messages
        ok, res = read_inbox()
        if not ok:
            print("❌ " + res)
            sys.exit(1)
        print(f"✅ {len(res)} message(s) reçu(s) ; champs : {sorted(res[0]) if res else 'aucun message'}")
        sys.exit(0)
    ok, detail = send(" ".join(sys.argv[1:]) or "Test HouseMonitor")
    print(("✅ " if ok else "❌ ") + detail)
    sys.exit(0 if ok else 1)
