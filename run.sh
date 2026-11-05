#!/bin/sh
# Kaicho on Linux: ./run.sh, ./run.sh --stop
exec python3 "$(dirname "$(readlink -f "$0")")/kaicho.py" "$@"
