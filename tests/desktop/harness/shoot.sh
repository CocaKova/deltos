#!/usr/bin/env bash
# Screenshot every scene in scenes.txt with a headless Chromium, one at a time.
#
#   CHROME=/path/to/headless_shell tests/desktop/harness/shoot.sh [out-dir] [name-filter]
#
# Run build.sh first. Needs only the browser binary (no node, no server): each
# scene is index.html opened from disk with its query string. The browser gets
# a throwaway profile that is removed afterwards.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
out="${1:-$here/shots}"
only="${2:-}"
chrome="${CHROME:-$(command -v chromium || command -v chromium-browser || command -v google-chrome || true)}"

if [ -z "$chrome" ] || [ ! -x "$chrome" ]; then
  echo "no browser: set CHROME=/path/to/headless_shell (or chromium)" >&2
  exit 2
fi
[ -f "$here/dist/harness.js" ] || { echo "run build.sh first" >&2; exit 2; }

mkdir -p "$out"
profile="$(mktemp -d)"
trap 'rm -rf "$profile"' EXIT

while IFS='|' read -r name size query; do
  name="$(echo "$name" | xargs)"
  case "$name" in ''|'#'*) continue ;; esac
  [ -n "$only" ] && [[ "$name" != *"$only"* ]] && continue
  size="$(echo "$size" | xargs)"
  query="$(echo "$query" | sed 's/^ *//; s/ *$//; s/ /%20/g; s/|/%7C/g')"
  "$chrome" --headless --no-sandbox --disable-gpu --hide-scrollbars --allow-file-access-from-files \
    --user-data-dir="$profile" --force-device-scale-factor=2 --window-size="${size/x/,}" \
    --virtual-time-budget=6000 --screenshot="$out/$name.png" \
    "file://$here/index.html?$query" >/dev/null 2>&1 </dev/null || echo "FAILED $name" >&2
  [ -f "$out/$name.png" ] && echo "$name.png"
done < "$here/scenes.txt"
