"""Data « internet seulement » du Pi, lue dans les compteurs nftables installés par setup-netcount.sh.

Ne compte que les échanges avec des adresses non locales : ce qui passe réellement par le routeur 4G.
Tout le Pi est inclus (Home Assistant, ce script, mises à jour système...).
"""
import fcntl
import json
import os
import subprocess

from common import HERE

STATE_FILE = HERE / "netcount_state.json"
LOCK_FILE = HERE / ".netcount.lock"
CMD = ["nft", "-j", "list", "counters", "table", "inet", "housemonitor"]


def raw():
    """Octets internet comptés depuis le chargement des règles (repart à 0 au reboot), None si non installé."""
    cmd = CMD if os.geteuid() == 0 else ["sudo", "-n"] + CMD
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=10, check=True).stdout
        return sum(o["counter"]["bytes"] for o in json.loads(out)["nftables"] if "counter" in o)
    except Exception:
        return None


def sample():
    """Cumul croissant des octets du Pi, qui survit aux reboots et rechargements des règles. None si non installé.

    Les compteurs du noyau repartent à 0 au reboot : une lecture plus petite que la précédente signifie
    « remise à zéro », on ajoute alors la valeur lue en entier. Les octets échangés entre la dernière lecture
    et un reboot sont perdus, d'où une lecture régulière par le listener."""
    with open(LOCK_FILE, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)  # listener et résumé peuvent lire en même temps
        r = raw()
        if r is None:
            return None
        try:
            st = json.loads(STATE_FILE.read_text())
            cum = st["cum"] + (r - st["last"] if r >= st["last"] else r)
        except (OSError, ValueError, KeyError):
            cum = r  # première lecture : trafic depuis l'installation
        STATE_FILE.write_text(json.dumps({"last": r, "cum": cum}))
        return cum
