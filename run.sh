#!/bin/sh
# Lancé par cron : git pull (mise à jour auto du script) puis envoi du résumé.
# Mesure les octets du git pull sur l'interface par défaut (4G) et les passe au script.
cd "$(dirname "$0")" || exit 1

IFACE=${IFACE:-$(ip route show default | awk '{print $5; exit}')}
rxtx() { awk -v i="$IFACE:" '$1==i {print $2+$10}' /proc/net/dev; }

b0=$(rxtx)
git pull -q --ff-only >> pull.log 2>&1
b1=$(rxtx)

echo "--- $(date '+%F %T') run" >> run.log
GIT_BYTES=$((b1 - b0)) python3 summary.py "$@" 2>> run.log
rc=$?
echo "exit $rc" >> run.log

# Crash avant l'envoi (HA injoignable, config cassée...) : le script n'a pas pu pinger lui-même
[ $rc -ne 0 ] && python3 -c 'from common import heartbeat; heartbeat(ok=False)' 
exit $rc
