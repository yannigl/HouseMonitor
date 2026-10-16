#!/bin/sh
# Lancé par cron : git pull (mise à jour auto du script) puis envoi du résumé.
# Mesure les octets du git pull sur l'interface par défaut (4G) et les passe au script.
#
# Tout est dans main(), appelée sur la dernière ligne : le shell lit le script en entier avant
# de l'exécuter, donc un `git pull` qui réécrit run.sh pendant le run ne peut pas le corrompre.

main() {
  cd "$(dirname "$0")" || return 1

  # Un seul run à la fois : une ligne cron en double ou un run manuel pendant celui de 7h30
  # ne doivent ni envoyer deux résumés ni doubler les pings.
  if command -v flock >/dev/null 2>&1; then
    exec 9> .run.lock
    flock -n 9 || { echo "--- $(date '+%F %T') run ignoré : un autre run est en cours" >> run.log; return 0; }
  fi

  PATH=/usr/local/sbin:/usr/sbin:/sbin:$PATH  # cron n'a pas sbin, où se trouve `ip`
  IFACE=${IFACE:-$(ip route show default 2>/dev/null | awk '{print $5; exit}')}
  # printf %.0f : pas de notation 1.2e+09 sur les gros compteurs (mawk), qui casserait le calcul
  rxtx() { awk -v i="$IFACE:" '$1==i {printf "%.0f", $2+$10}' /proc/net/dev; }

  b0=$(rxtx); b0=${b0:-0}
  update
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
  return $rc
}

# git pull horodaté, avec 3 essais (le DNS de la 4G flanche parfois). Un échec ne bloque pas le résumé.
update() {
  branch=$(git rev-parse --abbrev-ref HEAD)
  old=$(git rev-parse --short HEAD)
  for i in 1 2 3; do
    if out=$(git pull -q --ff-only origin "$branch" 2>&1); then
      new=$(git rev-parse --short HEAD)
      if [ "$old" = "$new" ]; then echo "$(date '+%F %T') pull ok : déjà à jour ($new)" >> pull.log
      else echo "$(date '+%F %T') pull ok : mis à jour $old -> $new" >> pull.log; fi
      return 0
    fi
    echo "$(date '+%F %T') pull ÉCHEC $i/3 : $(printf '%s' "$out" | tr '\n' ' ')" >> pull.log
    [ $i -lt 3 ] && sleep "${PULL_RETRY_DELAY:-15}"
  done
  return 1
}

main "$@"; exit $?
