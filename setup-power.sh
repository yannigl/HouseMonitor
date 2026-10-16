#!/bin/sh
# Autorise le listener à éteindre le Pi (commande Telegram /shutdown). À lancer une fois : sudo ./setup-power.sh
# Ajoute une règle sudoers limitée à UNE commande : `systemctl poweroff`.
set -eu

[ "$(id -u)" -eq 0 ] || { echo "À lancer avec sudo : sudo ./setup-power.sh"; exit 1; }
USER_NAME=${SUDO_USER:-}
[ -n "$USER_NAME" ] || { echo "Lancez-le avec sudo depuis votre utilisateur (pas directement en root)."; exit 1; }

SYSTEMCTL=$(command -v systemctl)
TMP=$(mktemp)
echo "$USER_NAME ALL=(root) NOPASSWD: $SYSTEMCTL poweroff" > "$TMP"
visudo -cf "$TMP" >/dev/null          # validée avant installation
install -m 440 "$TMP" /etc/sudoers.d/housemonitor-power
rm -f "$TMP"

if sudo -u "$USER_NAME" sudo -n -l systemctl poweroff >/dev/null 2>&1; then
  echo "OK : $USER_NAME peut exécuter 'systemctl poweroff' (rien d'autre). /shutdown est actif."
else
  echo "ATTENTION : règle installée mais non effective pour $USER_NAME (vérifiez /etc/sudoers.d/housemonitor-power)."
  exit 1
fi
echo "Désinstaller : sudo rm /etc/sudoers.d/housemonitor-power"
