#!/bin/sh
# Launch the bundled native engine from any working directory.
set -eu
server_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
case "$(/usr/bin/uname -m)" in
    arm64) arch=arm64 ;;
    x86_64) arch=x86_64 ;;
    *) echo 'Screen Organizer requires macOS arm64 or x86_64.' >&2; exit 1 ;;
esac
HOMESCREEN_READ_ONLY=${HOMESCREEN_READ_ONLY-1}
export HOMESCREEN_READ_ONLY
exec "$server_dir/$arch/engine" mcp "$@"
