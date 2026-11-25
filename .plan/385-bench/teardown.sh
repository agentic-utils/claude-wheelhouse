#!/bin/bash
HOME_REAL=$HOME; . "$(dirname "$0")/env.sh"
kill $(cat $SP/daemon.pid) $(cat $SP/sleeper.pid) 2>/dev/null
sleep 1
rm -rf $H $SP/daemon.pid $SP/sleeper.pid
