#!/bin/sh
set -eu
cd "$(dirname "$0")"
case "${1:-}" in
    ""|--configure) ;;
    *) echo "Usage: ./install.sh [--configure]" >&2; exit 2 ;;
esac
if [ -n "${REVIEW_IMAGE:-}" ]; then
    docker compose pull reviewer
else
    docker compose build reviewer
fi
configure=false
if [ "${1:-}" = --configure ]; then
    configure=true
elif docker compose run --rm --no-deps --entrypoint test setup -L /state/setup; then
    :
else
    code=$?
    [ "$code" -eq 1 ] || exit "$code"
    configure=true
fi
if [ "$configure" = true ]; then
    docker compose run --rm --no-deps --service-ports setup
    docker compose up -d --force-recreate sync reviewer
else
    docker compose run --rm --no-deps setup login
    docker compose up -d sync reviewer
fi
