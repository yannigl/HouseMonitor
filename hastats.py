"""Statistiques à long terme de Home Assistant (moyenne, minimum, maximum par jour ou par heure), lues par l'API WebSocket de HA.

HA garde l'historique détaillé 10 jours (recorder.purge_keep_days), mais ses statistiques à long terme, elles, ne sont pas supprimées. Cette API
n'existe qu'en WebSocket : petit client écrit ici avec la bibliothèque standard (poignée de main HTTP, trames avec masque, ping/pong).
Message utilisé : recorder/statistics_during_period (chaque entité à state_class : température, pression, humidité…).
"""
import base64
import hashlib
import json
import os
import socket
import ssl
import struct
from datetime import datetime
from urllib.parse import urlparse

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class WSError(Exception):
    pass


class WebSocket:
    def __init__(self, url, timeout=20):
        u = urlparse(url)
        secure = u.scheme in ("https", "wss")
        host, port = u.hostname, u.port or (443 if secure else 80)
        raw = socket.create_connection((host, port), timeout=timeout)
        self.sock = ssl.create_default_context().wrap_socket(raw, server_hostname=host) if secure else raw
        self.buf = b""
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((f"GET {u.path or '/'} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                           f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        head = self._until(b"\r\n\r\n")
        if b" 101 " not in head.split(b"\r\n")[0]:
            raise WSError(f"poignée de main refusée : {head.split(chr(13).encode())[0]!r}")
        accept = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
        if accept.lower().encode() not in head.lower():
            raise WSError("clé d'acceptation invalide")

    def _until(self, marker):
        while marker not in self.buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise WSError("connexion fermée")
            self.buf += chunk
        i = self.buf.index(marker) + len(marker)
        out, self.buf = self.buf[:i], self.buf[i:]
        return out

    def _read(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise WSError("connexion fermée")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def _send_frame(self, opcode, payload):
        header = bytes([0x80 | opcode])
        n = len(payload)
        if n < 126:
            header += bytes([0x80 | n])
        elif n < 65536:
            header += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            header += bytes([0x80 | 127]) + struct.pack(">Q", n)
        mask = os.urandom(4)
        self.sock.sendall(header + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def send_json(self, obj):
        self._send_frame(1, json.dumps(obj).encode())

    def recv_json(self):
        message = b""
        while True:
            b1, b2 = self._read(2)
            fin, opcode, n = b1 & 0x80, b1 & 0x0F, b2 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            mask = self._read(4) if b2 & 0x80 else None
            payload = self._read(n)
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 8:
                raise WSError("connexion fermée par HA")
            if opcode == 9:
                self._send_frame(10, payload)  # ping -> pong
                continue
            if opcode in (1, 0):
                message += payload
                if fin:
                    return json.loads(message)

    def close(self):
        try:
            self._send_frame(8, b"")
        except OSError:
            pass
        self.sock.close()


def _when(v):
    """Début de période : millisecondes depuis 1970 (HA récent) ou texte ISO (ancien) -> datetime local sans fuseau."""
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(v / 1000)
    return datetime.fromisoformat(v.replace("Z", "+00:00")).astimezone().replace(tzinfo=None)


def statistics(ha_url, token, entity_ids, start, end, period="day", units=None):
    """{entity_id: [(début, moyenne, minimum, maximum), ...]} sur [start, end] (datetimes avec fuseau). `units` : {"temperature": "°C"}."""
    ws = WebSocket(ha_url.replace("http", "ws", 1).rstrip("/") + "/api/websocket")
    try:
        first = ws.recv_json()
        if first.get("type") != "auth_required":
            raise WSError(f"réponse inattendue : {first.get('type')}")
        ws.send_json({"type": "auth", "access_token": token})
        auth = ws.recv_json()
        if auth.get("type") != "auth_ok":
            raise WSError("jeton refusé par HA")
        ws.send_json({"id": 1, "type": "recorder/statistics_during_period", "start_time": start.isoformat(), "end_time": end.isoformat(),
                      "statistic_ids": list(entity_ids), "period": period, "types": ["mean", "min", "max"], **({"units": units} if units else {})})
        while True:
            msg = ws.recv_json()
            if msg.get("id") == 1:
                break
        if not msg.get("success"):
            raise WSError(f"HA a refusé la demande : {msg.get('error', {}).get('message', msg)}")
        return {eid: [(_when(r["start"]), r.get("mean"), r.get("min"), r.get("max")) for r in rows]
                for eid, rows in msg["result"].items()}
    finally:
        ws.close()


def command(ha_url, token, payload):
    """Une commande WebSocket de HA (ex. {"type": "zha/devices"}) -> son résultat. Lève WSError si HA la refuse."""
    ws = WebSocket(ha_url.replace("http", "ws", 1).rstrip("/") + "/api/websocket")
    try:
        first = ws.recv_json()
        if first.get("type") != "auth_required":
            raise WSError(f"réponse inattendue : {first.get('type')}")
        ws.send_json({"type": "auth", "access_token": token})
        if ws.recv_json().get("type") != "auth_ok":
            raise WSError("jeton refusé par HA")
        ws.send_json({"id": 1, **payload})
        while True:
            msg = ws.recv_json()
            if msg.get("id") == 1:
                break
        if not msg.get("success"):
            raise WSError(f"HA a refusé la demande : {msg.get('error', {}).get('message', msg)}")
        return msg["result"]
    finally:
        ws.close()
