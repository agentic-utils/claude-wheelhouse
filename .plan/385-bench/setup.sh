#!/bin/bash
# setup.sh [items]: seed a scratch home and start its daemon (port 7461). teardown.sh ends it.
HOME_REAL=$HOME; . "$(dirname "$0")/env.sh"
ITEMS=${1:-4000}
[ -e $H ] && { echo "$H exists: run teardown.sh first"; exit 1; }
mkdir -p $H
setsid sleep 100000 > /dev/null 2>&1 & echo $! > $SP/sleeper.pid
$VENV $BENCH/seed.py $H $(cat $SP/sleeper.pid) $ITEMS | tail -1 > $SP/seed.json
setsid $VENV -m claude_wheelhouse daemon --port $PORT > $H/daemon.log 2>&1 &
echo $! > $SP/daemon.pid
for i in $(seq 50); do [ -s $H/state/daemon.token ] && break; sleep 0.2; done
sleep 1
$VENV $BENCH/layout.py http://127.0.0.1:$PORT $H/state/daemon.token > $SP/layout.json 2> $SP/layout.err
cat $SP/layout.json
