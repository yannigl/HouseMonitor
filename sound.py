"""/play : joue sur le haut-parleur du Pi la mélodie « Gran Vals » de Tárrega, la sonnerie des vieux Nokia.

La mélodie est calculée ici (onde carrée, fichier WAV dans /tmp), sans fichier son à installer. Le lecteur est cherché dans cet ordre :
  1. config.json -> "audio": {"command": ["aplay", "-q", "-D", "plughw:Headphones,0", "{file}"]} ({file} = le fichier WAV) ;
  2. le serveur audio de la session (PipeWire/PulseAudio) via `paplay`, si son socket existe pour cet utilisateur ;
  3. `aplay` (ALSA), avec "audio": {"device": "plughw:Headphones,0"} pour choisir la sortie jack (voir `aplay -l`).
"audio": {"volume": 0.6} règle l'amplitude du signal (0 à 1) ; le volume du haut-parleur et de la carte son se règle avec alsamixer.
"""
import math
import os
import shutil
import struct
import subprocess
import tempfile
import threading
import wave
from pathlib import Path

from common import CFG

CONF = CFG.get("audio", {})
VOLUME = min(1.0, max(0.05, float(CONF.get("volume", 0.6))))
RATE = 22050
UNIT = 0.11  # s par croche
# Gran Vals : (fréquence en Hz, durée en croches)
TUNE = [(659.26, 1), (587.33, 1), (369.99, 2), (415.30, 2), (554.37, 1), (493.88, 1), (293.66, 2), (329.63, 2),
        (493.88, 1), (440.00, 1), (277.18, 2), (329.63, 2), (440.00, 6)]
MAX_TIMES = 5
LOCK = threading.Lock()  # une seule sonnerie à la fois


def wav_path():
    return Path(tempfile.gettempdir()) / f"housemonitor-nokia-{int(VOLUME * 100)}.wav"


def make_wav(path):
    """Écrit la mélodie dans `path` (WAV mono 16 bits) si elle n'existe pas encore."""
    if path.exists():
        return path
    frames = bytearray()
    for freq, n in TUNE:
        length = int(n * UNIT * RATE)
        for i in range(length):
            t = i / RATE
            env = min(1.0, t / 0.005, (length - i) / (0.03 * RATE))  # attaque 5 ms, extinction 30 ms : pas de claquement
            sample = (1 if math.sin(2 * math.pi * freq * t) >= 0 else -1) * VOLUME * 0.5 * max(0.0, env)
            frames += struct.pack("<h", int(sample * 32767))
    frames += b"\0\0" * int(0.2 * RATE)
    tmp = path.with_suffix(".tmp")
    with wave.open(str(tmp), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(bytes(frames))
    tmp.replace(path)
    return path


def candidates(file):
    """Commandes de lecture à essayer, dans l'ordre : [(commande, environnement)]."""
    out = []
    env = dict(os.environ)
    if CONF.get("command"):
        out.append(([str(c).replace("{file}", str(file)) for c in CONF["command"]], env))
    runtime = f"/run/user/{os.getuid()}"
    if os.path.exists(f"{runtime}/pulse/native") and shutil.which("paplay"):
        out.append((["paplay", str(file)], {**env, "XDG_RUNTIME_DIR": runtime, "PULSE_SERVER": f"unix:{runtime}/pulse/native"}))
    if shutil.which("aplay"):
        cmd = ["aplay", "-q"] + (["-D", CONF["device"]] if CONF.get("device") else []) + [str(file)]
        out.append((cmd, env))
    return out


def play(times=1):
    """Lance la sonnerie `times` fois (1 à 5) sans bloquer. -> (ok, détail). Un échec immédiat du lecteur est signalé."""
    times = max(1, min(MAX_TIMES, int(times)))
    if not LOCK.acquire(blocking=False):
        return False, "la sonnerie est déjà en cours"
    try:
        file = make_wav(wav_path())
        errors = []
        for cmd, env in candidates(file):
            try:
                proc = subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
            except OSError as e:
                errors.append(f"{cmd[0]} : {e}")
                continue
            try:
                rc = proc.wait(timeout=1.5)
            except subprocess.TimeoutExpired:
                rc = None  # toujours en train de jouer : c'est bon signe
            if rc not in (None, 0):
                errors.append(f"{cmd[0]} : {(proc.stderr.read() or '').strip()[-200:] or f'code {rc}'}")
                continue

            def rest(first=proc, cmd=cmd, env=env):
                try:
                    first.wait()
                    for _ in range(times - 1):
                        subprocess.run(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
                except Exception:
                    pass
                finally:
                    LOCK.release()
            threading.Thread(target=rest, daemon=True, name="play").start()
            return True, f"sonnerie lancée ({cmd[0]}){f', {times} fois' if times > 1 else ''}"
        LOCK.release()
        if not candidates(file):
            return False, "aucun lecteur audio (installez-le : sudo apt install alsa-utils)"
        return False, "le lecteur audio a échoué : " + " | ".join(errors)
    except Exception as e:
        LOCK.release()
        return False, f"{type(e).__name__}: {e}"
