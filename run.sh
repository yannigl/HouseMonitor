#!/bin/sh
# Lancé par cron : git pull (mise à jour auto du script) puis envoi du résumé.
# Mesure les octets du git pull sur l'interface par défaut (4G) et les passe au script.
cd "$(dirname "$0")" || exit 1
PATH=/usr/local/sbin:/usr/sbin:/sbin:$PATH  # cron n'a pas sbin, où se trouve `ip`

IFACE=${IFACE:-$(ip route show default 2>/dev/null | awk '{print $5; exit}')}
# printf %.0f : pas de notation 1.2e+09 sur les gros compteurs (mawk), qui casserait le calcul
rxtx() { awk -v i="$IFACE:" '$1==i {printf "%.0f", $2+$10}' /proc/net/dev; }

b0=$(rxtx); b0=${b0:-0}
git pull -q --ff-only origin "$(git rev-parse --abbrev-ref HEAD)" >> pull.log 2>&1
b1=$(rxtx); b1=${b1:-0}

echo "--- $(date '+%F %T') run" >> run.log
GIT_BYTES=$((b1 - b0)) python3 summary.py "$@" 2>> run.log
rc=$?
echo "exit $rc" >> run.log

# 3 = summary.py a déjà signalé l'échec (avec la trace). Autre code ≠ 0 = Python n'a pas pu démarrer
# (import cassé, syntaxe...) : on signale ici, avec la fin de run.log en message.
if [ $rc -ne 0 ] && [ $rc -ne 3 ]; then
  tail -n 40 run.log | python3 -c 'import sys; from common import heartbeat; heartbeat(ok=False, body=sys.stdin.read())'
fi
exit $rc
