#!/usr/bin/env python3
"""Diagnostic DNS (stdlib seulement) : python3 dnscheck.py

1. quelles routes sortantes existent (IPv4 / IPv6) ;
2. ce que résout le système, c'est-à-dire ce que voient ping, git et le script (5 essais par nom,
   pour voir si les échecs sont intermittents) ;
3. ce que répond CHAQUE serveur DNS, interrogé directement : ceux de /etc/resolv.conf et deux publics.
   Si les publics répondent et pas le routeur, le problème vient du serveur DNS du routeur.
"""
import os
import random
import socket
import struct
import time

NAMES = ["github.com", "www.google.com", "api.telegram.org", "hc-ping.com"]
PUBLIC = [("1.1.1.1", "Cloudflare"), ("9.9.9.9", "Quad9")]
PORT = int(os.environ.get("DNSCHECK_PORT", 53))
TIMEOUT = 2.5
RCODES = {0: "ok", 1: "FORMERR", 2: "SERVFAIL", 3: "NXDOMAIN", 4: "NOTIMP", 5: "REFUSED"}


def outgoing(family, probe):
    """Adresse source choisie pour sortir (sans rien envoyer : connect UDP), ou None si pas de route."""
    try:  # le noyau peut ne pas avoir d'IPv6 du tout : la création du socket échoue alors
        s = socket.socket(family, socket.SOCK_DGRAM)
    except OSError:
        return None
    try:
        s.connect((probe, 53))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def nameservers():
    try:
        return [l.split()[1] for l in open("/etc/resolv.conf") if l.startswith("nameserver")]
    except OSError:
        return []


def query(server, name, qtype):
    """Requête DNS UDP brute. Retourne (rcode, nb de réponses, ms) ; lève socket.timeout / OSError."""
    tid = random.randrange(65536)
    q = struct.pack(">HHHHHH", tid, 0x0100, 1, 0, 0, 0)
    q += b"".join(bytes([len(p)]) + p.encode() for p in name.split(".")) + b"\x00" + struct.pack(">HH", qtype, 1)
    fam, typ, _, _, addr = socket.getaddrinfo(server, PORT, 0, socket.SOCK_DGRAM)[0]
    s = socket.socket(fam, typ)
    s.settimeout(TIMEOUT)
    t = time.time()
    try:
        s.sendto(q, addr)
        data, _ = s.recvfrom(4096)
    finally:
        s.close()
    rid, flags, _, an = struct.unpack(">HHHH", data[:8])
    if rid != tid:
        raise OSError("réponse inattendue")
    return flags & 0xF, an, (time.time() - t) * 1000


def describe(server, name, qtype):
    try:
        rcode, an, ms = query(server, name, qtype)
        return f"{RCODES.get(rcode, rcode)}{f' ({an} rép.)' if rcode == 0 else ''} {ms:.0f}ms"
    except socket.timeout:
        return f"TIMEOUT >{TIMEOUT:.1f}s"
    except OSError as e:
        return f"ERREUR {e.strerror or e}"


def main():
    print("== Routes sortantes")
    v4, v6 = outgoing(socket.AF_INET, "1.1.1.1"), outgoing(socket.AF_INET6, "2606:4700:4700::1111")
    print(f"IPv4 : {v4 or 'AUCUNE route'}")
    print(f"IPv6 : {v6 or 'AUCUNE route'}")

    ns = nameservers()
    print(f"\n== Serveurs DNS du système (/etc/resolv.conf) : {', '.join(ns) or 'AUCUN'}")

    print("\n== Résolution par le système (ce que voient ping, git, le script) : 5 essais par nom")
    for name in NAMES:
        ok, fams, slow = 0, set(), 0.0
        for _ in range(5):
            t = time.time()
            try:
                for ai in socket.getaddrinfo(name, 443, proto=socket.IPPROTO_TCP):
                    fams.add("IPv6" if ai[0] == socket.AF_INET6 else "IPv4")
                ok += 1
            except socket.gaierror:
                pass
            slow = max(slow, time.time() - t)
            time.sleep(0.4)
        print(f"{name:18s} {ok}/5 OK   adresses : {'+'.join(sorted(fams)) or '-'}   (plus lent : {slow:.1f}s)")

    print("\n== Chaque serveur DNS interrogé directement (A / AAAA)")
    for server, label in [(s, "système") for s in ns] + [(s, l) for s, l in PUBLIC if s not in ns]:
        dead = False
        for name in NAMES:
            if dead:
                break
            a = describe(server, name, 1)
            if a.startswith("TIMEOUT"):  # serveur muet : inutile d'attendre 8 fois
                print(f"{server:22s} ({label}) {name:18s} A: {a}   -> ne répond pas, suite ignorée")
                dead = True
                continue
            print(f"{server:22s} ({label}) {name:18s} A: {a:22s} AAAA: {describe(server, name, 28)}")


if __name__ == "__main__":
    main()
