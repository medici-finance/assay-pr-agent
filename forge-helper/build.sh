#!/bin/sh
set -eu
assay=$(cd "${1:?path to the Assay source checkout}" && pwd)
output=${2:?absolute path to output binary}
cd "$(dirname "$0")"
workspace=$(mktemp -d)
trap 'rm -rf "$workspace"' EXIT
export GOWORK="$workspace/go.work"
go work init . "$assay/tools/desk"
CGO_ENABLED=0 go build -trimpath -o "$output" .
