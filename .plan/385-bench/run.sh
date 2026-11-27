#!/bin/bash
# run.sh <label> [ptybench args]: one ptybench run against the scratch daemon (setup.sh first). Results in $SP/<label>.json
HOME_REAL=$HOME; . "$(dirname "$0")/env.sh"
L=$1; shift
$VENV $BENCH/ptybench.py $SP/layout.json $SP/$L.json --label $L --daemon-pid $(cat $SP/daemon.pid) "$@"
