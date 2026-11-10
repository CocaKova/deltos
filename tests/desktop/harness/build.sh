#!/usr/bin/env bash
# Build the visual harness: one classic script (dist/harness.js) holding real
# React, the real Memory page, the stand-in SDK and the fixtures, so index.html
# opens straight from disk: no server, no node at viewing time.
#
#   HARNESS_MODULES=/path/to/node_modules tests/desktop/harness/build.sh
#
# HARNESS_MODULES is a node_modules folder with `react` and `react-dom` (19.x).
# Optional, for a closer look: `@vscode/codicons` (the app's icon font),
# `@fontsource-variable/inter` and `@fontsource-variable/jetbrains-mono`.
# Without them the page falls back to system fonts and icons stay blank.
# esbuild: $ESBUILD, PATH, or HARNESS_MODULES/.bin/esbuild.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
modules="${HARNESS_MODULES:-$here/node_modules}"
esbuild="${ESBUILD:-$(command -v esbuild || echo "$modules/.bin/esbuild")}"

if [ ! -d "$modules/react" ] || [ ! -d "$modules/react-dom" ]; then
  echo "react and react-dom not found: set HARNESS_MODULES=/path/to/node_modules" >&2
  exit 2
fi
if [ ! -x "$esbuild" ]; then
  echo "esbuild not found: put it on PATH or set ESBUILD=/path/to/esbuild" >&2
  exit 2
fi

mkdir -p "$here/dist"
NODE_PATH="$modules" "$esbuild" "$here/main.mjs" \
  --bundle --format=iife --minify --log-level=warning \
  --outfile="$here/dist/harness.js" \
  --alias:@hermes/plugin-sdk="$here/sdk.mjs" \
  --define:process.env.NODE_ENV='"production"'

icons="$modules/@vscode/codicons/dist"
if [ -f "$icons/codicon.css" ]; then
  sed 's/codicon\.ttf?[0-9a-f]*/codicon.ttf/' "$icons/codicon.css" > "$here/dist/codicon.css"
  cp "$icons/codicon.ttf" "$here/dist/codicon.ttf"
else
  : > "$here/dist/codicon.css"
fi
sans="$modules/@fontsource-variable/inter/files/inter-latin-wght-normal.woff2"
mono="$modules/@fontsource-variable/jetbrains-mono/files/jetbrains-mono-latin-wght-normal.woff2"
[ -f "$sans" ] && cp "$sans" "$here/dist/sans.woff2"
[ -f "$mono" ] && cp "$mono" "$here/dist/mono.woff2"

echo "built $here/dist  (open index.html?fixture=demo&theme=dark)"
