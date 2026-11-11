#!/bin/sh
# Start memory watchdog in background, then hand over to the original entrypoint.
python /opt/mem_watchdog.py &
exec container-entrypoint "$@"
