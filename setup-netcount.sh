#!/bin/sh
# Installe le compteur « internet seulement » du Pi (nftables). À lancer une fois : sudo ./setup-netcount.sh
#
# Compte les octets échangés avec des adresses NON locales (hors 10/8, 172.16/12, 192.168/16, link-local,
# multicast, et pour l'IPv6 hors fe80::/10, fc00::/7, ff00::/8 et le préfixe du réseau local), donc ce qui
# traverse réellement le routeur 4G. Le trafic local avec vos appareils n'est pas compté.
# Trois points de comptage : input/output (HA en réseau "host", scripts) et forward (conteneurs en bridge).
# Les compteurs se lisent via `sudo nft` ; une règle sudoers limitée à cette seule lecture est installée.
set -eu

BIN=/usr/local/sbin/housemonitor-netcount
UNIT=/etc/systemd/system/housemonitor-netcount.service
SUDOERS=/etc/sudoers.d/housemonitor-netcount

ruleset() {
  # Préfixes IPv6 globaux du Pi (réseau local) à exclure : le trafic LAN en IPv6 public n'est pas de la 4G.
  prefixes=$(ip -6 -o addr show scope global 2>/dev/null | awk '{print $4}' | python3 -c '
import ipaddress, sys
print(", ".join(sorted({str(ipaddress.ip_interface(l.strip()).network) for l in sys.stdin if l.strip()})))' || true)
  extra6=${prefixes:+, $prefixes}
  cat <<NFT
table inet housemonitor
delete table inet housemonitor
table inet housemonitor {
  set local4 {
    type ipv4_addr
    flags interval
    elements = { 0.0.0.0/8, 10.0.0.0/8, 127.0.0.0/8, 169.254.0.0/16, 172.16.0.0/12, 192.168.0.0/16, 224.0.0.0/4, 255.255.255.255 }
  }
  set local6 {
    type ipv6_addr
    flags interval
    elements = { ::/128, ::1, fc00::/7, fe80::/10, ff00::/8${extra6} }
  }
  counter in4 { }
  counter out4 { }
  counter in6 { }
  counter out6 { }
  chain input {
    type filter hook input priority 0; policy accept;
    ip saddr != @local4 counter name "in4"
    ip6 saddr != @local6 counter name "in6"
  }
  chain output {
    type filter hook output priority 0; policy accept;
    ip daddr != @local4 counter name "out4"
    ip6 daddr != @local6 counter name "out6"
  }
  chain forward {
    type filter hook forward priority 0; policy accept;
    ip daddr != @local4 counter name "out4"
    ip saddr != @local4 counter name "in4"
    ip6 daddr != @local6 counter name "out6"
    ip6 saddr != @local6 counter name "in6"
  }
}
NFT
}

case "${1:-install}" in
  --print) ruleset; exit 0 ;;                       # affiche le jeu de règles (sans l'appliquer)
  --load)  ruleset | nft -f -; exit 0 ;;            # appelé au démarrage par systemd
esac

[ "$(id -u)" -eq 0 ] || { echo "À lancer avec sudo : sudo ./setup-netcount.sh"; exit 1; }
USER_NAME=${SUDO_USER:-}
[ -n "$USER_NAME" ] || { echo "Lancez-le avec sudo depuis votre utilisateur (pas directement en root)."; exit 1; }

if ! command -v nft >/dev/null 2>&1; then
  echo "nftables absent : installation (quelques centaines de Ko)..."
  apt-get install -y nftables
fi
NFT=$(command -v nft)

# Copie root du script : le service systemd n'exécute pas un fichier modifiable par un utilisateur
install -m 755 "$0" "$BIN"

cat > "$UNIT" <<UNIT_EOF
[Unit]
Description=HouseMonitor : compteurs de data internet (nftables)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=$BIN --load

[Install]
WantedBy=multi-user.target
UNIT_EOF
systemctl daemon-reload
systemctl enable --now housemonitor-netcount.service

# sudoers : uniquement la lecture des compteurs, validée avant d'être installée
TMP=$(mktemp)
echo "$USER_NAME ALL=(root) NOPASSWD: $NFT -j list counters table inet housemonitor" > "$TMP"
visudo -cf "$TMP" >/dev/null
install -m 440 "$TMP" "$SUDOERS"
rm -f "$TMP"

# Vérification, comme le fera le listener
if sudo -u "$USER_NAME" sudo -n nft -j list counters table inet housemonitor >/dev/null 2>&1; then
  echo "OK : compteurs en place. Lecture par $USER_NAME fonctionnelle."
else
  echo "ATTENTION : l'installation est faite mais la lecture par $USER_NAME échoue (vérifiez $SUDOERS)."
  exit 1
fi
echo "Si le préfixe IPv6 de votre routeur change durablement, relancez : sudo ./setup-netcount.sh"
