#!/bin/bash
# pin-daemon.sh [core]: restart the scratch daemon pinned to one core (default 13); "" for unpinned.
HOME_REAL=$HOME; . "$(dirname "$0")/env.sh"
CORE=${1-13}
kill $(cat $SP/daemon.pid) 2>/dev/null; sleep 1.5
rm -f $H/state/daemon.token.ready
setsid ${CORE:+taskset -c $CORE} $VENV -m claude_wheelhouse daemon --port $PORT >> $H/daemon.log 2>&1 &
echo $! > $SP/daemon.pid
sleep 3; taskset -cp $(cat $SP/daemon.pid); tail -2 $H/daemon.log
