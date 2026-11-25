#!/bin/sh
set -eu
cd "$(dirname "$0")"
case "${1:-}" in
    ""|--configure) ;;
    *) echo "Usage: ./install.sh [--configure]" >&2; exit 2 ;;
esac
if [ -n "${REVIEW_IMAGE:-}" ]; then
    docker compose up -d --pull always --no-build
else
    docker compose up -d --build
fi
echo 'Press Enter to open setup, or type q and press Enter to quit.'
exec docker compose attach setup
