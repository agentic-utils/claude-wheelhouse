# #385 bench environment: sourced by setup.sh / the run scripts. A scratch HOME and state dir, its own daemon port.
CODE=${CODE:-/home/doug/projects/claude-wheelhouse/.worktrees/385-ui-perf-review}
BENCH=$CODE/.plan/385-bench
SP=${SP:-$HOME_REAL/.claude/scratchpad/385-ui-perf-review}
VENV=${VENV:-/home/doug/projects/claude-wheelhouse/.venv/bin/python}
PORT=${PORT:-7461}
H=$SP/home
unset WHEELHOUSE_DB WHEELHOUSE_SESSION_ID WHEELHOUSE_TOKEN VIRTUAL_ENV
export HOME=$H WHEELHOUSE_DB=$H/state/wheelhouse.db PYTHONPATH=$CODE WHEELHOUSE_PORT=$PORT
