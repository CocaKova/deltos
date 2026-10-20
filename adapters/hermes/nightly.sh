#!/usr/bin/env bash
# Nightly memory job: the import as a staged run (new note lines become facts on a copy of
# memory; the run is judged against this store's own accepted runs, then promoted or held with
# its numbers), the learn sweep of chats that ended while nothing was listening (`deltos learn
# --wait`: only with [learn] auto, and once the agent is idle), the chat digests, a commit of
# the day's other writes, and the status line.
#
#   DELTOS_SRC    the deltos checkout to run (default: the repo this script sits in)
#   DELTOS_ROOT   the memory folder (default ~/deltos)
#   DELTOS_SITE   this machine's own steps, run first when the file exists, e.g. checkpoint
#                 commits of the note repos the import reads (default ~/.config/deltos/nightly-site.sh).
#                 It runs in its own shell without `set -u`, so it defines every variable it uses;
#                 a failed step is logged and the import still runs.
#
# A held import exits 0: the hold file ($ROOT/import.hold) and the status line are the signal
# (deltos run show|promote|discard <id>). Only a failed import exits non-zero; memory is untouched then.
# A held or failed learn sweep is logged ($ROOT/learn.hold) and never changes the exit code.
set -u
SRC=${DELTOS_SRC:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}
ROOT=${DELTOS_ROOT:-$HOME/deltos}
LOG=$ROOT/nightly.log
log() { echo "$(date -Iseconds) $*" >> "$LOG"; }
SITE=${DELTOS_SITE:-$HOME/.config/deltos/nightly-site.sh}
if [ -f "$SITE" ]; then ( set +u; . "$SITE" ) || log "site steps failed rc=$? (the import runs anyway)"; fi

cd "$SRC" || exit 1
deltos() { python3 -m deltos --root "$ROOT" "$@"; }
deltos import >> "$LOG" 2>&1
rc=$?
case $rc in
  0) if [ -f "$ROOT/import.hold" ]; then log "import still held: $(head -1 "$ROOT/import.hold")"; else log "import ok"; fi ;;
  3) log "import HELD: $(head -1 "$ROOT/import.hold")"; rc=0 ;;
  *) log "import FAILED rc=$rc (memory untouched)" ;;
esac
deltos learn --wait >> "$LOG" 2>&1
lrc=$?
case $lrc in
  0) ;;
  3) log "learn HELD: $(head -1 "$ROOT/learn.hold")" ;;
  *) log "learn FAILED rc=$lrc" ;;
esac
deltos sessions --days 2 >> "$LOG" 2>&1

# The import and the learn sweep committed their own runs; this is the digests and the day's agent
# and hand writes.
if [ -e "$ROOT/.git" ]; then
  (cd "$ROOT" && git add -A && { git -c user.name=deltos -c user.email=deltos@local commit -qm "sessions + writes $(date +%F)" || true; })
fi
deltos status --line >> "$LOG" 2>&1
tail -n 2000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
exit $rc
