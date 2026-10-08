#!/bin/sh
set -eu
if [ "$#" -lt 1 ]; then
    printf '%s\n' 'Usage: sh install.sh /absolute/path/to/python3 --root /absolute/install/root [options]' >&2
    exit 2
fi
patchport_python=$1
shift
case "$patchport_python" in
    /*) ;;
    *) printf '%s\n' 'Select an absolute Python 3.11+ executable with venv and ensurepip.' >&2; exit 2 ;;
esac
exec "$patchport_python" -I -B "$(dirname "$0")/bootstrap.py" "$@"
