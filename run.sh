#!/bin/sh
# Irodori on Linux: ./run.sh, ./run.sh --stop
exec python3 "$(dirname "$(readlink -f "$0")")/irodori.py" "$@"
