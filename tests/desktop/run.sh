#!/usr/bin/env bash
# Bundle the Memory page (adapters/hermes/deltos/desktop/plugin.js) against a stub
# SDK and walk every state it can be in, answered from the demo-store fixtures.
# No app, no gateway, no browser: the page is plain ESM, so esbuild + node is
# the whole harness.
#
#   tests/desktop/run.sh                       # from anywhere
#   ESBUILD=/path/to/esbuild tests/desktop/run.sh
#
# esbuild is looked for on PATH, then in a Hermes checkout. Fixtures are made by
# tests/desktop/make_fixtures.py (re-run it when the backend's answers change).
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
root="$(cd "$here/../.." && pwd)"
esbuild="${ESBUILD:-$(command -v esbuild || echo "$HOME/.hermes/hermes-agent/node_modules/.bin/esbuild")}"

if [ ! -x "$esbuild" ]; then
  echo "esbuild not found: put it on PATH or set ESBUILD=/path/to/esbuild" >&2
  exit 2
fi

"$esbuild" "$root/adapters/hermes/deltos/desktop/plugin.js" \
  --bundle --format=esm --log-level=warning \
  --outfile="$here/deltos-bundle.mjs" \
  --alias:@hermes/plugin-sdk="$here/stub-sdk.mjs" \
  --alias:react/jsx-runtime="$here/stub-jsx.mjs" \
  --alias:react="$here/stub-jsx.mjs"

node "$here/smoke.mjs"
