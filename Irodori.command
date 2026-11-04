#!/bin/sh
# Irodori on macOS: double-click in Finder.
cd "$(dirname "$0")" && exec python3 irodori.py "$@"
